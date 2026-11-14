"""AGENT 4 · EXECUTOR. Never picks a coin. Does arithmetic and sends 1 button.
FOMO has no public order API: never places an order, never touches funds.

Output -> db.signals (+ db.positions on fill), pubsub "signal"/"fill",
fill -> Activity "Bought X / Executor · {time} / ${size} · {pct}% of stack" -> bus.executor_to_risk.
"""

from __future__ import annotations

import asyncio
from typing import Any

import bus
import config
import logutil
from links import coin_url
from notifier import callback_button, fomo_button
from runtime import Ctx, coin_label, local_hhmm, money

log = logutil.get("executor")


def size_ticket(stack: float, free: float, liquidity: float | None, cfg: dict) -> dict[str, Any]:
    """SIZE. -> {"size", "raw", "cap", "skip"}; skip is a reason or None."""
    raw = stack * cfg["executor.stack_pct"] / 100.0
    size, cap = raw, "stack_pct"
    pool_cap = (liquidity or 0) * cfg["executor.max_pool_pct"] / 100.0
    if pool_cap < size:
        size, cap = pool_cap, "max_pool_pct"
    minimum = cfg["executor.min_ticket_usd"]
    if size < minimum:
        if cfg["executor.below_min"] == "skip":
            return {"size": round(size, 2), "raw": raw, "cap": cap, "skip": f"under ${minimum:.2f}"}
        size, cap = minimum, cap + "+bump"
    if size > free + 1e-9:
        return {"size": round(size, 2), "raw": raw, "cap": cap, "skip": "no free stack"}
    return {"size": round(size, 2), "raw": raw, "cap": cap, "skip": None}


def open_money(conn) -> float:
    r = conn.execute("SELECT COALESCE(SUM(size_usd),0) FROM positions WHERE closed_at IS NULL").fetchone()
    return float(r[0] or 0)


def scanner_liquidity(conn, mint: str, fallback: float | None) -> float | None:
    r = conn.execute(
        "SELECT liquidity_usd FROM scans WHERE mint=? AND verdict='pass' ORDER BY id DESC LIMIT 1", (mint,)
    ).fetchone()
    return r[0] if r and r[0] is not None else fallback


class Executor:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.trades = 0
        self.waiting: dict[str, asyncio.Future] = {}

    def _record(self, out: dict) -> None:
        log.info("\t".join(str(out.get(k)) for k in
                           ("mint", "_stack", "_raw", "_cap", "sizeUsd", "mode", "status", "reason", "link")))

    async def handle(self, msg: dict) -> dict | None:
        ctx, conn = self.ctx, self.ctx.conn
        cfg = ctx.cfg()
        mode = cfg["mode"]
        mint = msg["analyst"]["mint"]
        night = ctx.night_start()
        link = coin_url(mint, "solana")
        if conn.execute("SELECT 1 FROM signals WHERE mint=? AND ts>=?", (mint, night)).fetchone():
            log.info(f"{mint}\tignored\talready signalled tonight")
            return None  # 1 push per coin per night, a 2nd Analyst pass is ignored
        if conn.execute("SELECT 1 FROM positions WHERE mint=? AND closed_at IS NULL", (mint,)).fetchone():
            log.info(f"{mint}\tignored\topen position")
            return None  # never a 2nd buy on an open position
        if conn.execute("SELECT 1 FROM skips WHERE mint=?", (mint,)).fetchone():
            return None
        stack = config.current_stack(conn, cfg)
        free = stack - open_money(conn)
        try:
            dex = (await ctx.src.dex.tokens([mint])).get(mint) or {}
        except Exception as e:  # noqa: BLE001
            log.info(f"{mint}\tdex_error\t{type(e).__name__}")
            dex = {}
        price, liq = dex.get("priceUsd"), dex.get("liquidityUsd")
        sz = size_ticket(stack, free, liq, cfg)
        out = {"agent": "executor", "mint": mint, "signalPrice": price, "sizeUsd": sz["size"],
               "stackPct": round(sz["size"] / stack * 100, 2) if stack else None, "link": link,
               "mode": mode, "status": "skipped", "reason": None,
               "_stack": round(stack, 2), "_raw": round(sz["raw"], 2), "_cap": sz["cap"]}

        async def skip(reason: str) -> dict:
            out["reason"] = reason
            conn.execute(
                "INSERT INTO signals(mint, ts, price, size_usd, stack_pct, link, mode, status)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (mint, ctx.now_iso(), price, out["sizeUsd"], out["stackPct"], link, mode, "skipped"),
            )
            conn.commit()
            self._record(out)
            bus.publish("signal", {k: v for k, v in out.items() if not k.startswith("_")})
            return out

        if not price:
            return await skip("no price")
        if sz["skip"]:
            return await skip(sz["skip"])
        # RE-CHECK right before the push
        liq_scan = scanner_liquidity(conn, mint, msg["scanner"].get("liquidityUsd"))
        if liq_scan and liq is not None and liq < liq_scan * (1 - cfg["executor.recheck_liq_drop_pct"] / 100):
            return await skip("liquidity dropping")
        p_an = msg.get("priceAtAnalyst")
        if p_an and price > p_an * (1 + cfg["executor.recheck_price_up_pct"] / 100):
            return await skip("ran before the push")

        name = coin_label(conn, mint)
        pct = out["stackPct"]
        text = (f"Buy {name} · ${money(sz['size'])} · {pct:g}% of stack\n"
                f"Auditor 8/8 · Analyst {msg['analyst']['score']} · stop -{cfg['risk.stop_pct']:g}% · "
                f"trail {cfg['risk.trail_pct']:g}%")
        buttons = [fomo_button("Buy on FOMO", mint), callback_button("Bought", "bought", mint),
                   callback_button("Skip", "skip", mint)]
        ok = await ctx.notifier.push(text, mode=mode, mint=mint, buttons=buttons, kind="buy", retries=3)
        ts = ctx.now_iso()
        sig_id = conn.execute(
            "INSERT INTO signals(mint, ts, price, size_usd, stack_pct, link, mode, status)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (mint, ts, price, sz["size"], pct, link, mode, "sent"),
        ).lastrowid
        conn.commit()
        out["status"] = "sent"
        if not ok:
            log.info(f"{mint}\tpush failed")
            out["reason"] = "push failed"
            if mode == "LIVE":
                conn.execute("UPDATE signals SET status='missed' WHERE id=?", (sig_id,))
                conn.commit()
                out["status"] = "missed"
                self._record(out)
                bus.publish("signal", {k: v for k, v in out.items() if not k.startswith("_")})
                return out
        bus.publish("signal", {k: v for k, v in out.items() if not k.startswith("_")})
        if mode == "PAPER":
            return await self.fill(out, sig_id, name, ts)
        # LIVE: [Bought] opens the position at the signal price; no tap in N min = missed.
        fut = asyncio.get_running_loop().create_future()
        self.waiting[mint] = fut
        try:
            action = await asyncio.wait_for(fut, timeout=cfg["executor.confirm_window_min"] * 60)
        except asyncio.TimeoutError:
            action = "missed"
        finally:
            self.waiting.pop(mint, None)
        if action == "bought":
            return await self.fill(out, sig_id, name, ctx.now_iso())
        if action == "skip":
            conn.execute("INSERT OR REPLACE INTO skips(mint, until) VALUES (?, 'morning report')", (mint,))
        conn.execute("UPDATE signals SET status=? WHERE id=?",
                     ("skipped" if action == "skip" else "missed", sig_id))
        conn.commit()
        out["status"] = "skipped" if action == "skip" else "missed"
        self._record(out)
        return out

    async def fill(self, out: dict, sig_id: int, name: str, ts: str) -> dict:
        ctx, conn = self.ctx, self.ctx.conn
        cfg = ctx.cfg()
        entry = out["signalPrice"]
        stop = entry * (1 - cfg["risk.stop_pct"] / 100)
        pos_id = conn.execute(
            "INSERT INTO positions(mint, opened_at, entry_price, size_usd, peak_price, stop_price,"
            " trail_active, mode) VALUES (?,?,?,?,?,?,?,?)",
            (out["mint"], ts, entry, out["sizeUsd"], entry, stop, 0, out["mode"]),
        ).lastrowid
        conn.execute("UPDATE signals SET status='bought' WHERE id=?", (sig_id,))
        conn.execute(
            "INSERT INTO activity(ts, kind, mint, agent, title, subtitle, value) VALUES (?,?,?,?,?,?,?)",
            (ts, "bought", out["mint"], "executor", f"Bought {name}", f"Executor · {local_hhmm(ts)}",
             f"${money(out['sizeUsd'])} · {out['stackPct']:g}% of stack"),
        )
        conn.commit()
        self.trades += 1
        out["status"] = "bought"
        self._record(out)
        bus.publish("fill", {k: v for k, v in out.items() if not k.startswith("_")} | {"positionId": pos_id})
        await bus.executor_to_risk.put(pos_id)
        return out

    async def callbacks_loop(self) -> None:
        while True:
            action, mint = await bus.executor_callbacks.get()
            fut = self.waiting.get(mint)
            if fut and not fut.done():
                fut.set_result(action)
            elif action == "skip":
                self.ctx.conn.execute("INSERT OR REPLACE INTO skips(mint, until) VALUES (?, 'morning report')",
                                      (mint,))
                self.ctx.conn.commit()

    async def run(self) -> None:
        cb = asyncio.create_task(self.callbacks_loop())
        tasks: set[asyncio.Task] = set()
        try:
            while True:
                bus.beat("executor")
                try:
                    msg = await asyncio.wait_for(bus.analyst_to_executor.get(), timeout=15)
                except asyncio.TimeoutError:
                    continue
                while bus.is_paused("executor"):
                    bus.beat("executor")
                    await asyncio.sleep(2)
                if self.ctx.cfg()["mode"] == "PAPER":
                    await self._safe(msg)  # serial: free stack can't be double-spent
                else:
                    t = asyncio.create_task(self._safe(msg))  # LIVE waits for a tap, others go on
                    tasks.add(t)
                    t.add_done_callback(tasks.discard)
        finally:
            cb.cancel()

    async def _safe(self, msg):
        try:
            await self.handle(msg)
        except Exception as e:  # noqa: BLE001
            log.info(f"{msg.get('analyst', {}).get('mint')}\terror\t{type(e).__name__}: {e}")


async def run(ctx: Ctx) -> None:
    await Executor(ctx).run()
