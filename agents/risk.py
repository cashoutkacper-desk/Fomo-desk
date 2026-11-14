"""AGENT 5 · RISK. Decides exits and sends the Sell button. Never places an order.
Exit rules don't bend. Never moves a stop down. Can't be paused with a position open.

Output (1 JSON per exit) -> db.positions, db.activity, pubsub "exit" (+ "balance" in PAPER).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import bus
import config
import fees
import logutil
from db import insert_price
from links import coin_url
from notifier import callback_button, fomo_button
from runtime import Ctx, coin_label, money, parse_iso

log = logutil.get("risk")


def stop_pct_of(pos: dict) -> float:
    """The stop this position carries, as a % (from its stop line)."""
    return round((1 - pos["stop_price"] / pos["entry_price"]) * 100, 2)


def step(pos: dict, price: float, now: datetime, cfg: dict) -> dict[str, Any]:
    """RULES, every poll, in order. Mutates pos (peak, stop_price, trail_active).
    -> {"exit": reason|None, "stop_line", "trail_line"}."""
    entry = pos["entry_price"]
    # a tighter stop setting applies to open positions; a looser one never does
    tighter = entry * (1 - cfg["risk.stop_pct"] / 100)
    if tighter > pos["stop_price"] + 1e-15:
        pos["stop_price"] = tighter
    pos["peak_price"] = max(pos["peak_price"] or price, price)
    trail_line = pos["peak_price"] * (1 - cfg["risk.trail_pct"] / 100)
    if price <= pos["stop_price"]:
        return {"exit": "stop", "stop_line": pos["stop_price"], "trail_line": trail_line}
    if price >= entry * (1 + cfg["risk.trail_start_pct"] / 100):
        pos["trail_active"] = 1  # never turns off
    if pos["trail_active"] and price <= trail_line:
        return {"exit": "trail", "stop_line": pos["stop_price"], "trail_line": trail_line}
    hours = (now - parse_iso(pos["opened_at"])).total_seconds() / 3600
    if hours >= cfg["risk.timeout_h"] and price < entry * (1 + cfg["risk.timeout_min_gain_pct"] / 100):
        return {"exit": "timeout", "stop_line": pos["stop_price"], "trail_line": trail_line}
    return {"exit": None, "stop_line": pos["stop_price"], "trail_line": trail_line}


def exit_line(pos: dict, cfg: dict) -> float:
    """The current exit line for the Coin screen's orange 'Risk trailing stop'."""
    if pos["trail_active"]:
        return max(pos["stop_price"], pos["peak_price"] * (1 - cfg["risk.trail_pct"] / 100))
    return pos["stop_price"]


def pnl(pos: dict, exit_price: float, discount: float) -> tuple[float, float, float]:
    """-> (pnl_usd net of fees on both sides, pct of size, fees_usd)."""
    size = pos["size_usd"]
    exit_value = size * exit_price / pos["entry_price"]
    f = fees.round_trip_fees(size, exit_value, discount)
    p = exit_value - size - f
    return round(p, 2), round(p / size * 100, 2), round(f, 2)


class Risk:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.no_price: dict[int, int] = {}
        self.last_exit: str | None = None
        self.exiting: dict[str, dict] = {}     # LIVE: mint -> pending exit
        self.missed_polls = 0

    def open_positions(self) -> list[dict]:
        rows = self.ctx.conn.execute(
            "SELECT p.*, c.name, c.symbol FROM positions p LEFT JOIN coins c ON c.mint=p.mint "
            "WHERE p.closed_at IS NULL AND p.exit_reason IS NULL"
        ).fetchall()
        return [dict(r) for r in rows]

    async def poll_once(self) -> list[dict]:
        ctx, conn = self.ctx, self.ctx.conn
        cfg = ctx.cfg()
        bus.beat("risk")
        positions = self.open_positions()
        if not positions:
            return []
        mints = sorted({p["mint"] for p in positions})
        views = await ctx.src.dex.tokens(mints)   # all open mints in 1 call
        now = ctx.now()
        ts = ctx.now_iso()
        for m in mints:
            v = views.get(m)
            if v:
                insert_price(conn, "risk", m, ts, v.get("priceUsd"), v.get("volumeM5"))
        conn.commit()
        exits = []
        for pos in positions:
            v = views.get(pos["mint"]) or {}
            price, liq = v.get("priceUsd"), v.get("liquidityUsd")
            name = pos.get("name") or pos.get("symbol") or coin_label(conn, pos["mint"])
            if liq is not None and liq <= 0:
                last = self.last_price(pos["mint"]) or pos["entry_price"]
                exits.append(await self.exit(pos, "pulled", last, name))
                continue
            if not price:
                n = self.no_price.get(pos["id"], 0) + 1
                self.no_price[pos["id"]] = n
                log.info(f"{pos['mint']}\tno price\tpoll {n}")
                if n == cfg["risk.no_price_polls"]:
                    await ctx.notifier.push(f"{name}: no price for 1 min", mode=pos["mode"], mint=pos["mint"],
                                            buttons=[fomo_button("Sell on FOMO", pos["mint"])], kind="alert")
                continue  # never invent a price
            self.no_price.pop(pos["id"], None)
            old_stop = pos["stop_price"]
            r = step(pos, price, now, cfg)
            conn.execute("UPDATE positions SET peak_price=?, stop_price=?, trail_active=? WHERE id=?",
                         (pos["peak_price"], pos["stop_price"], pos["trail_active"], pos["id"]))
            conn.commit()
            if pos["stop_price"] != old_stop:
                log.info(f"{pos['mint']}\tstop tightened\t{old_stop:.10g} -> {pos['stop_price']:.10g}")
            log.info(f"{pos['mint']}\tprice {price:.10g}\tpeak {pos['peak_price']:.10g}\t"
                     f"stop {r['stop_line']:.10g}\ttrail {r['trail_line']:.10g}"
                     f"{'(on)' if pos['trail_active'] else '(off)'}\t{r['exit'] or 'hold'}")
            if r["exit"]:
                exits.append(await self.exit(pos, r["exit"], price, name))
        return exits

    def last_price(self, mint: str) -> float | None:
        r = self.ctx.conn.execute(
            "SELECT price FROM prices WHERE mint=? AND price IS NOT NULL ORDER BY ts DESC LIMIT 1", (mint,)
        ).fetchone()
        return r[0] if r else None

    async def exit(self, pos: dict, reason: str, exit_price: float, name: str) -> dict:
        """exit price = the polled price that fired it, even if it gapped past the line."""
        ctx, conn = self.ctx, self.ctx.conn
        cfg = ctx.cfg()
        mode = pos["mode"]
        sp = stop_pct_of(pos)
        p, pct, f = pnl(pos, exit_price, cfg["fees_ref_discount"])
        ts = ctx.now_iso()
        if reason == "trail":
            text = f"Sold {name} · +${money(p)} (+{pct:g}%)" if p >= 0 else f"Sold {name} · -${money(p)} ({pct:g}%)"
        elif reason == "stop":
            text = f"{name} hit the -{sp:g}% stop · -${money(p)}"
        elif reason == "timeout":
            text = f"Sell {name}, dead money · {pct:g}% in {cfg['risk.timeout_h']:g}h"
        else:
            text = f"{name} liquidity pulled · sell now · {'+' if p >= 0 else '-'}${money(p)}"
        buttons = [fomo_button("Sell on FOMO", pos["mint"])]
        if mode == "LIVE":
            buttons.append(callback_button("Sold", "sold", pos["mint"]))
        await ctx.notifier.push(text, mode=mode, mint=pos["mint"], buttons=buttons, kind="exit")
        out = {"agent": "risk", "mint": pos["mint"], "name": name, "ts": ts, "reason": reason,
               "entryPrice": pos["entry_price"], "exitPrice": exit_price, "peakPrice": pos["peak_price"],
               "pnlUsd": p, "pct": pct, "feesUsd": f, "stopPct": sp, "mode": mode,
               "status": "closed" if mode == "PAPER" else "exiting", "link": coin_url(pos["mint"], "solana")}
        if mode == "PAPER":
            self.close(pos, reason, exit_price, p, f, ts, name, sp, pct)
        else:
            conn.execute("UPDATE positions SET exit_reason=?, exit_price=? WHERE id=?",
                         (reason, exit_price, pos["id"]))
            conn.commit()
            self.exiting[pos["mint"]] = {"pos": pos, "out": out, "since": ctx.now(), "repushed": False,
                                         "text": text, "buttons": buttons}
        self.last_exit = "Trailed out" if reason == "trail" else "Stopped" if reason == "stop" else reason
        log.info(f"{pos['mint']}\tEXIT {reason}\tentry {pos['entry_price']:.10g}\texit {exit_price:.10g}\t"
                 f"pnl {p}\t{pct}%\tfees {f}\tstop -{sp:g}%\t{mode}\t{out['status']}")
        bus.publish("exit", out)
        return out

    def close(self, pos, reason, exit_price, p, f, ts, name, sp, pct) -> None:
        conn = self.ctx.conn
        conn.execute(
            "UPDATE positions SET closed_at=?, exit_price=?, exit_reason=?, pnl_usd=?, fees_usd=? WHERE id=?",
            (ts, exit_price, reason, p, f, pos["id"]),
        )
        if reason == "stop":
            kind, sub, val = "stopped", f"Risk · -{sp:g}% stop", f"-${money(p)}"
        elif reason == "pulled":
            kind, sub, val = "stopped", "Risk · liquidity pulled", f"{'+' if p >= 0 else '-'}${money(p)}"
        else:
            kind = "sold"
            sub = "Risk · trailing stop" if reason == "trail" else "Risk · timeout"
            sign = "+" if p >= 0 else "-"
            val = f"{sign}${money(p)} · {sign}{abs(pct):g}%"
        title = ("Stopped " if kind == "stopped" else "Sold ") + name
        conn.execute(
            "INSERT INTO activity(ts, kind, mint, agent, title, subtitle, value) VALUES (?,?,?,?,?,?,?)",
            (ts, kind, pos["mint"], "risk", title, sub, val),
        )
        if pos["mode"] == "PAPER":
            cfg = self.ctx.cfg()
            bal = config.current_stack(conn, cfg) + p
            conn.execute("INSERT INTO balance(ts, value) VALUES (?, ?)", (ts, round(bal, 2)))
            bus.publish("balance", {"ts": ts, "value": round(bal, 2)})
        conn.commit()

    async def live_loop(self) -> None:
        """LIVE: [Sold] closes; no tap in N min -> push again once, 'still open'."""
        while True:
            try:
                action, mint = await asyncio.wait_for(bus.risk_callbacks.get(), timeout=10)
            except asyncio.TimeoutError:
                action, mint = None, None
            if action == "sold" and mint in self.exiting:
                e = self.exiting.pop(mint)
                o = e["out"]
                self.close(e["pos"], o["reason"], o["exitPrice"], o["pnlUsd"], o["feesUsd"],
                           self.ctx.now_iso(), o["name"], o["stopPct"], o["pct"])
                bus.publish("exit", o | {"status": "closed"})
            cfg = self.ctx.cfg()
            for m, e in list(self.exiting.items()):
                waited = (self.ctx.now() - e["since"]).total_seconds() / 60
                if not e["repushed"] and waited >= cfg["risk.confirm_window_min"]:
                    e["repushed"] = True
                    await self.ctx.notifier.push(e["text"] + " · still open", mode="LIVE", mint=m,
                                                 buttons=e["buttons"], kind="exit")

    async def run(self) -> None:
        live = asyncio.create_task(self.live_loop())
        try:
            while True:
                cfg = self.ctx.cfg()
                try:
                    await asyncio.wait_for(self.poll_once(), 60)   # wall-clock cap on 1 poll
                except Exception as e:  # noqa: BLE001  (TimeoutError lands here too)
                    from sources import RateLimited
                    if isinstance(e, RateLimited):
                        missed = max(1, cfg["risk.backoff_429_s"] // cfg["risk.poll_s"])
                        self.missed_polls += missed
                        log.info(f"dexscreener_429\tback off {cfg['risk.backoff_429_s']}s\tmissed polls {missed}")
                        await asyncio.sleep(cfg["risk.backoff_429_s"])
                        continue
                    log.info(f"poll_error\t{type(e).__name__}: {e}")
                bus.beat("risk")
                # wake early when Executor hands over a new position
                try:
                    await asyncio.wait_for(bus.executor_to_risk.get(), timeout=cfg["risk.poll_s"])
                except asyncio.TimeoutError:
                    pass
        finally:
            live.cancel()


async def run(ctx: Ctx) -> None:
    await Risk(ctx).run()
