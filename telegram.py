"""STEP 9 · Telegram and talking to the desk.

Long polling (getUpdates), owner only: every update from any chat other than
settings.owner_chat_id is ignored (no reply, no answerCallbackQuery). If no owner is
set yet, the first chat that sends /start becomes owner_chat_id (STEP 2 owner lock).

Pushes are sent by the agents through notifier.py ("PAPER · " prefix in PAPER).
This module adds:
  - the [Bought] / [Skip] / [Sold] callbacks -> bus.executor_callbacks / risk_callbacks
  - the commands below, as /command or plain text

  buy X      X pushed tonight: resend that push with [Buy on FOMO]. Never pushed: run
             Auditor on it now (on demand, nothing goes to Analyst), reply with the 8
             layers + [Buy on FOMO] and "not sized, not on the desk". No order is placed.
  sell X     [Sell on FOMO] for the open X position
  why X      which agent stopped X, the layer or rule, the value, the time
  pause A / resume A   any agent (Risk not while a position is open)
  set K to V any setting by name or alias, through config.set_value (validated, change_log)
  stack N    LIVE stack (config.set_value("live_stack"))
  status     mode, balance, open positions, each with [Sell on FOMO]
  paper | live   switch mode through config.set_mode; live needs the owner to type LIVE
  invite     SIGNUP_URL
  report     link to the last morning Page
X = name, symbol or mint. Unknown name/symbol: ask for the mint. 2+ matches: ask which,
each with its coin_url. Unknown text: the command list. Every coin in a reply carries its
coin_url (links.py). The token is never printed or logged.

The same handler runs from the box for the Grok Bot app:  python telegram.py "status"
(prints the reply with the full links.py URLs under it; sends nothing).
"""

from __future__ import annotations

import asyncio
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

import bus
import config
import logutil
from db import utc_now
from links import SIGNUP_URL, coin_url
from runtime import coin_label, iso, local_hhmm, money, parse_iso

log = logutil.get("telegram")
MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
LIVE_CONFIRM_S = 120
BACKLOG_MAX_S = 600            # updates older than this at startup are skipped, not run
AGENT_NAMES = {a: a for a in bus.AGENTS}

HELP = """Commands (as /command or plain text):
buy X — resend tonight's push with [Buy on FOMO], or audit X now
sell X — [Sell on FOMO] for your open X position
why X — which agent stopped X, the layer or rule, the value, the time
pause Scanner / resume Scanner — any agent (Risk not while a position is open)
set stop to -8% — any setting by name or alias
stack 1240 — LIVE stack
status — mode, balance, open positions
paper | live — switch mode (live needs you to type LIVE)
invite — FOMO invite link
report — link to the last morning Page
X can be a name, symbol or mint."""

Reply = dict[str, Any]   # {"text": str, "buttons": [...], "push_mode": "PAPER"|"LIVE"|None}


def _r(text: str, buttons: list | None = None, push_mode: str | None = None) -> Reply:
    return {"text": text, "buttons": buttons or [], "push_mode": push_mode}


def fomo(text: str, mint: str, row: int | None = None) -> dict:
    b = {"text": text, "url": coin_url(mint, "solana")}
    if row is not None:
        b["row"] = row
    return b


class Desk:
    """Command handler. Pure db + config + bus; no network except the on-demand audit."""

    def __init__(self, conn, auditor: Any = None, dex: Any = None,
                 clock: Callable[[], datetime] | None = None,
                 on_change: Callable[[], Awaitable[None] | None] | None = None, log_dir=None):
        self.conn = conn
        self.auditor = auditor          # agents.auditor.Auditor (for "buy X" never pushed)
        self.dex = dex                  # sources.Dex (name/pool/liquidity for that audit)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.on_change = on_change      # plan B: republish after a change
        self.live_pending_until: datetime | None = None
        self.log_dir = log_dir or config.LOG_DIR

    # ------------------------------------------------------------ helpers
    def cfg(self) -> dict:
        return config.load(self.conn)

    def night(self) -> str:
        return config.night_start(self.cfg(), self.clock())

    def one(self, sql, args=()):
        r = self.conn.execute(sql, args).fetchone()
        return dict(r) if r is not None else None

    def name(self, mint: str) -> str:
        return coin_label(self.conn, mint)

    def resolve(self, x: str) -> tuple[str | None, Reply | None]:
        """-> (mint, None) or (None, reply asking for the mint / which one)."""
        x = x.strip().lstrip("$").strip()
        if not x:
            return None, _r("Which coin? Send a name, symbol or mint.")
        if MINT_RE.match(x):
            return x, None
        rows = self.conn.execute(
            "SELECT mint, name, symbol FROM coins WHERE lower(symbol)=lower(?) OR lower(name)=lower(?) "
            "ORDER BY migrated_at DESC", (x, x)).fetchall()
        if not rows:
            return None, _r(f"I don't know {x}. Send me its mint.")
        if len(rows) == 1:
            return rows[0][0], None
        lines = [f"{x} matches {len(rows)} coins. Which one? Send the mint:"]
        buttons = []
        for i, (m, n, s) in enumerate(rows[:8]):
            lines.append(f"{n or s or '?'} ({s or '?'}) · {m}")
            buttons.append(fomo(f"{n or s} · {m[:4]}...{m[-3:]}", m, row=i))
        return None, _r("\n".join(lines), buttons)

    async def changed(self) -> None:
        if self.on_change:
            try:
                r = self.on_change()
                if asyncio.iscoroutine(r):
                    await r
            except Exception as e:  # noqa: BLE001
                log.info(f"republish after change failed: {type(e).__name__}")

    # ------------------------------------------------------------ entry
    async def handle(self, text: str) -> list[Reply]:
        raw = (text or "").strip()
        if self.live_pending_until and raw == "LIVE":
            return await self.confirm_live()
        t = raw[1:] if raw.startswith("/") else raw
        parts = t.split(None, 1)
        if not parts:
            return [_r(HELP)]
        cmd = parts[0].split("@", 1)[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd != "live":
            self.live_pending_until = None
        try:
            if cmd == "buy":
                return await self.buy(arg)
            if cmd == "sell":
                return self.sell(arg)
            if cmd == "why":
                return self.why(arg)
            if cmd in ("pause", "resume"):
                return await self.pause(cmd, arg)
            if cmd == "set":
                return await self.set(arg)
            if cmd == "stack":
                return await self.stack(arg)
            if cmd == "status":
                return self.status()
            if cmd == "paper" and not arg:
                return await self.paper()
            if cmd == "live" and not arg:
                return self.live()
            if cmd == "invite":
                return [_r(f"Sign up for FOMO through my link:\n{SIGNUP_URL}")]
            if cmd == "report":
                return self.report()
        except config.SettingError as e:
            return [_r(f"Not changed: {e}")]
        return [_r(HELP)]

    # ------------------------------------------------------------ buy / sell / why
    def last_push(self, mint: str) -> dict | None:
        return self.one("SELECT * FROM signals WHERE mint=? AND ts>=? AND status IN ('sent','bought','missed') "
                        "ORDER BY id DESC LIMIT 1", (mint, self.night()))

    async def buy(self, x: str) -> list[Reply]:
        mint, ask = self.resolve(x)
        if ask:
            return [ask]
        sig = self.last_push(mint)
        name = self.name(mint)
        if sig:
            cfg = self.cfg()
            v = self.one("SELECT score FROM verdicts WHERE mint=? AND pass=1 ORDER BY id DESC LIMIT 1", (mint,))
            score = f"{v['score']:g}" if v and v["score"] is not None else "?"
            text = (f"Buy {name} · ${money(sig['size_usd'])} · {sig['stack_pct']:g}% of stack\n"
                    f"Auditor 8/8 · Analyst {score} · stop -{cfg['risk.stop_pct']:g}% · trail {cfg['risk.trail_pct']:g}%")
            return [_r(text, [fomo("Buy on FOMO", mint)], push_mode=sig["mode"])]
        if self.auditor is None:
            return [_r(f"{name} wasn't pushed tonight, and the Auditor isn't running here. "
                       f"Not sized, not on the desk.", [fomo("Buy on FOMO", mint)])]
        out = await self.audit_now(mint)
        name = self.name(mint)
        icon = {True: "✅", False: "❌", None: "·"}
        lines = [f"{name} · Auditor now · " + ("Cleared · 8/8 parts" if out["passed"] else f"Rejected · {out['reason']}")]
        for l in out["layers"]:
            lines.append(f"{icon[l['pass']]} {l['n']} {l['name']} · {l['value']}")
        lines.append("Not sized, not on the desk. No order is placed.")
        return [_r("\n".join(lines), [fomo("Buy on FOMO", mint)])]

    async def audit_now(self, mint: str) -> dict:
        view = {}
        if self.dex is not None:
            try:
                view = (await self.dex.tokens([mint])).get(mint) or {}
            except Exception as e:  # noqa: BLE001
                log.info(f"{mint}\tdex for on-demand audit failed: {type(e).__name__}")
        if view:
            created = view.get("pairCreatedAt")
            mig = iso(datetime.fromtimestamp(created / 1000, tz=timezone.utc)) if isinstance(created, (int, float)) else None
            self.conn.execute("INSERT OR IGNORE INTO coins(mint, name, symbol, pool, migrated_at) VALUES (?,?,?,?,?)",
                              (mint, view.get("name"), view.get("symbol"), view.get("pairAddress"), mig))
            self.conn.commit()
        self.conn.execute("INSERT OR IGNORE INTO coins(mint) VALUES (?)", (mint,))   # audits reference coins
        self.conn.commit()
        pool = view.get("pairAddress") or (self.one("SELECT pool FROM coins WHERE mint=?", (mint,)) or {}).get("pool")
        scan = {"agent": "telegram", "mint": mint, "name": view.get("name"), "pool": pool,
                "liquidityUsd": view.get("liquidityUsd")}
        log.info(f"{mint}\ton-demand audit (buy X, never pushed)")
        return await self.auditor.audit(scan, forward=False)

    def sell(self, x: str) -> list[Reply]:
        mint, ask = self.resolve(x)
        if ask:
            return [ask]
        name = self.name(mint)
        pos = self.one("SELECT * FROM positions WHERE mint=? AND closed_at IS NULL ORDER BY id DESC LIMIT 1", (mint,))
        if not pos:
            return [_r(f"No open {name} position.", [fomo("Open on FOMO", mint)])]
        last = self.one("SELECT price FROM prices WHERE mint=? AND price IS NOT NULL ORDER BY ts DESC LIMIT 1", (mint,))
        since = ""
        if last and pos["entry_price"]:
            p = (last["price"] / pos["entry_price"] - 1) * 100
            since = f" · {'+' if p >= 0 else ''}{p:.1f}% since entry"
        prefix = "PAPER · " if pos["mode"] == "PAPER" else ""
        return [_r(f"{prefix}Sell {name} · ${money(pos['size_usd'])} in{since}", [fomo("Sell on FOMO", mint)])]

    def _executor_skip_reason(self, mint: str) -> str | None:
        files = sorted(self.log_dir.glob("executor.log*"), reverse=True)
        for f in files:
            try:
                lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                cols = line.split("\t")
                if mint in cols and "skipped" in cols:
                    i = cols.index("skipped")
                    if i + 1 < len(cols) and cols[i + 1] not in ("None", ""):
                        return cols[i + 1]
        return None

    def why(self, x: str) -> list[Reply]:
        mint, ask = self.resolve(x)
        if ask:
            return [ask]
        name = self.name(mint)
        btn = [fomo("Open on FOMO", mint)]
        pos = self.one("SELECT * FROM positions WHERE mint=? ORDER BY id DESC LIMIT 1", (mint,))
        if pos and pos["closed_at"]:
            pnl = pos["pnl_usd"] or 0
            return [_r(f"{name}: closed by Risk · rule {pos['exit_reason']} · exit {pos['exit_price']:.10g} · "
                       f"{'+' if pnl >= 0 else '-'}${money(pnl)} · {local_hhmm(pos['closed_at'])}", btn)]
        if pos:
            return [_r(f"{name} wasn't stopped: the position is open (Risk guarding since "
                       f"{local_hhmm(pos['opened_at'])}).", btn)]
        sig = self.one("SELECT * FROM signals WHERE mint=? ORDER BY id DESC LIMIT 1", (mint,))
        if sig and sig["status"] in ("skipped", "missed"):
            why = self._executor_skip_reason(mint) if sig["status"] == "skipped" else "no [Bought] tap in time"
            return [_r(f"{name}: stopped by Executor · {sig['status']} · {why or 'reason not logged'} · "
                       f"{local_hhmm(sig['ts'])}", btn)]
        v = self.one("SELECT * FROM verdicts WHERE mint=? ORDER BY id DESC LIMIT 1", (mint,))
        if v and not v["pass"]:
            return [_r(f"{name}: stopped by Analyst · rule {v['reason']} · score {v['score'] if v['score'] is None else format(v['score'], 'g')} · "
                       f"{local_hhmm(v['ts'])}", btn)]
        a = self.one("SELECT * FROM audits WHERE mint=? AND pass=0 ORDER BY id DESC LIMIT 1", (mint,))
        if a:
            return [_r(f"{name}: stopped by Auditor · layer {a['layer']} {a['name']} · {a['value_text']} · "
                       f"{local_hhmm(a['ts'])}", btn)]
        s = self.one("SELECT * FROM scans WHERE mint=? ORDER BY id DESC LIMIT 1", (mint,))
        if s and s["verdict"] != "pass":
            return [_r(f"{name}: stopped by Scanner · {s['reason'] or s['verdict']} · {local_hhmm(s['ts'])}", btn)]
        if sig or v or s or self.one("SELECT 1 x FROM audits WHERE mint=? LIMIT 1", (mint,)):
            return [_r(f"{name} wasn't stopped by any agent.", btn)]
        return [_r(f"The desk hasn't seen {name}.", btn)]

    # ------------------------------------------------------------ control
    async def pause(self, cmd: str, arg: str) -> list[Reply]:
        agent = arg.strip().lower()
        if agent not in AGENT_NAMES:
            return [_r(f"Which agent? {', '.join(a.capitalize() for a in bus.AGENTS)}.")]
        old = "paused" if bus.is_paused(agent) else "running"
        if cmd == "pause":
            open_n = self.conn.execute("SELECT COUNT(*) FROM positions WHERE closed_at IS NULL").fetchone()[0]
            try:
                bus.pause(agent, open_n)
            except ValueError as e:
                return [_r(f"Not paused: {e}.")]
            new = "paused"
        else:
            bus.resume(agent)
            new = "running"
        self.conn.execute("INSERT INTO change_log(ts, key, old, new, who) VALUES (?,?,?,?,?)",
                          (utc_now(), f"agent.{agent}", old, new, "telegram"))
        self.conn.commit()
        await self.changed()
        return [_r(f"{agent.capitalize()} {old} -> {new}")]

    _SET_RE = re.compile(r"^(?P<k>.+?)\s*(?:\s+to\s+|=|:|\s+)\s*(?P<v>\S+)$", re.I)

    async def set(self, arg: str) -> list[Reply]:
        m = self._SET_RE.match(arg.strip())
        if not m:
            return [_r("Send it like: set stop to -8%")]
        key, val = m.group("k").strip(), m.group("v").strip()
        val = val.rstrip("%").replace("$", "").replace(",", "") if re.match(r"^-?\$?[\d,.]+%?$", val) else val
        if config.resolve_key(key) == "mode":
            return [_r("Mode switches with: paper | live")]
        r = config.set_value(self.conn, key, val, who="telegram")
        out = r["message"]
        if r.get("ticket") and r["ticket"].get("message"):     # bump: tell; skip: warn
            out += "\n" + r["ticket"]["message"]
        await self.changed()
        return [_r(out)]

    async def stack(self, arg: str) -> list[Reply]:
        v = arg.strip().replace("$", "").replace(",", "")
        if not v:
            return [_r(f"LIVE stack is ${money(self.cfg()['live_stack'])}. Send: stack 1240")]
        r = config.set_value(self.conn, "live_stack", v, who="telegram")
        out = f"stack ${money(r['old'])} -> ${money(r['new'])}"
        if r.get("ticket") and r["ticket"].get("message"):
            out += "\n" + r["ticket"]["message"]
        await self.changed()
        return [_r(out)]

    def status(self) -> list[Reply]:
        cfg = self.cfg()
        bal = config.current_stack(self.conn, cfg)
        pos = [dict(r) for r in self.conn.execute("SELECT * FROM positions WHERE closed_at IS NULL ORDER BY opened_at")]
        head = f"{cfg['mode']} · balance ${money(bal)} · {len(pos)} open position{'s' if len(pos) != 1 else ''}"
        out = [_r(head)]
        for p in pos:
            name = self.name(p["mint"])
            out.append(_r(f"{name} · ${money(p['size_usd'])} in · since {local_hhmm(p['opened_at'])} · "
                          f"stop {p['stop_price']:.10g}", [fomo("Sell on FOMO", p["mint"])]))
        return out

    async def paper(self) -> list[Reply]:
        r = config.set_mode(self.conn, "PAPER", who="telegram")
        await self.changed()
        return [_r(r["message"])]

    def live(self) -> list[Reply]:
        if self.cfg()["mode"] == "LIVE":
            return [_r("Already LIVE.")]
        if not config.full_paper_night(self.conn):
            return [_r("Not changed: LIVE needs at least 1 full PAPER night in the db first.")]
        self.live_pending_until = self.clock() + timedelta(seconds=LIVE_CONFIRM_S)
        return [_r("Type LIVE to switch to LIVE (within 2 minutes). Anything else cancels.")]

    async def confirm_live(self) -> list[Reply]:
        until, self.live_pending_until = self.live_pending_until, None
        if until is None or self.clock() > until:
            return [_r("Too late, nothing changed. Send live again.")]
        r = config.set_mode(self.conn, "LIVE", confirm="LIVE", who="telegram")
        await self.changed()
        return [_r(r["message"])]

    def report(self) -> list[Reply]:
        row = self.one("SELECT value FROM settings WHERE key='last_report_url'")
        if row and row["value"]:
            return [_r(f"Last morning Page:\n{row['value']}")]
        return [_r("No morning Page yet.")]


class Bot:
    """Long polling, owner only. Replies through the TelegramNotifier (owner chat only)."""

    def __init__(self, notifier, desk: Desk, poll_timeout_s: int = 50):
        self.n = notifier
        self.desk = desk
        self.poll_timeout_s = poll_timeout_s
        self.offset: int | None = None
        self.started = datetime.now(timezone.utc)
        self.ignored = 0

    def _chat_of(self, u: dict) -> tuple[Any, dict | None, dict | None]:
        msg = u.get("message") or u.get("edited_message")
        cq = u.get("callback_query")
        if msg:
            return (msg.get("chat") or {}).get("id"), msg, None
        if cq:
            return ((cq.get("message") or {}).get("chat") or {}).get("id") or (cq.get("from") or {}).get("id"), None, cq
        return None, None, None

    async def send(self, replies: list[Reply]) -> None:
        for r in replies:
            if r.get("push_mode"):
                await self.n.push(r["text"], mode=r["push_mode"], buttons=r["buttons"], kind="resend")
            else:
                await self.n.message(r["text"], kind="reply", buttons=r["buttons"])

    async def on_update(self, u: dict) -> None:
        chat, msg, cq = self._chat_of(u)
        if self.n.owner_chat_id is None and msg and (msg.get("text") or "").strip().split("@")[0] == "/start":
            from db import set_setting
            set_setting(self.desk.conn, "owner_chat_id", str(int(chat)), "telegram /start")
            self.desk.conn.commit()
            self.n.owner_chat_id = int(chat)
            log.info("owner set by first /start")
        if not self.n.is_owner(chat):
            self.ignored += 1
            log.info("ignored update from a non-owner chat")
            return
        if msg is not None:
            date = msg.get("date")
            if date and (self.started - datetime.fromtimestamp(date, tz=timezone.utc)).total_seconds() > BACKLOG_MAX_S:
                log.info("skipped an old command from before the desk started")
                return
            text = msg.get("text") or ""
            log.info(f"command: {text[:60]}")
            if text.strip().split("@")[0] == "/start":
                await self.send([_r(HELP)])
                return
            await self.send(await self.desk.handle(text))
        elif cq is not None:
            data = cq.get("data") or ""
            action, _, mint = data.partition(":")
            note = "?"
            if action in ("bought", "skip") and MINT_RE.match(mint):
                await bus.executor_callbacks.put((action, mint))
                note = "Bought, noted" if action == "bought" else "Skipped for tonight"
            elif action == "sold" and MINT_RE.match(mint):
                await bus.risk_callbacks.put((action, mint))
                note = "Sold, noted"
            log.info(f"callback {action} {mint}")
            try:
                await self.n._call("answerCallbackQuery", {"callback_query_id": cq.get("id"), "text": note})
            except Exception as e:  # noqa: BLE001
                log.info(f"answerCallbackQuery failed: {type(e).__name__}")

    async def poll_once(self) -> int:
        payload: dict[str, Any] = {"timeout": self.poll_timeout_s, "allowed_updates": ["message", "callback_query"]}
        if self.offset is not None:
            payload["offset"] = self.offset
        body = await self.n._call("getUpdates", payload, timeout=self.poll_timeout_s + 15)
        ups = body.get("result") or []
        for u in ups:
            self.offset = int(u["update_id"]) + 1
            try:
                await self.on_update(u)
            except Exception as e:  # noqa: BLE001
                log.info(f"update failed: {type(e).__name__}: {str(e)[:120]}")
        return len(ups)

    async def run(self) -> None:
        log.info("polling started")
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001  (TelegramError never carries the token)
                log.info(f"poll failed: {type(e).__name__}: {str(e)[:120]}")
                await asyncio.sleep(5)


async def run_polling(conn, notifier, auditor=None, dex=None, on_change=None) -> None:
    await Bot(notifier, Desk(conn, auditor=auditor, dex=dex, on_change=on_change)).run()


def _cli(argv: list[str]) -> int:
    """python telegram.py "status"  -> the same reply, printed (Grok Bot app path). Sends nothing.
    Pause/resume only reach agents running in the desk process; use Telegram for those."""
    import db
    from notifier import render_plain

    text = " ".join(argv).strip()
    if not text:
        print(HELP)
        return 0
    conn = db.connect()
    desk = Desk(conn)
    for r in asyncio.run(desk.handle(text)):
        print(render_plain(r) if r["buttons"] else r["text"])
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
