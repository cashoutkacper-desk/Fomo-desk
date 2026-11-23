"""AGENT 1 · SCANNER. Counts and cuts. Never judges quality.

Owns every coin that graduates off the pumpfun curve and lands on PumpSwap.
Input: PumpPortal migrations (1 websocket) + on-chain backup feed (the pump.fun migration
authority's transactions over our Solana RPC, Kacper Nov 23), DexScreener batches, Solana RPC.
Output: 1 JSON per decision -> db.scans + app (pubsub "scan"); pass -> bus.scanner_to_auditor.
"""

from __future__ import annotations

import asyncio
import json
import unicodedata
from datetime import datetime, timezone
from typing import Any

import bus
import fields
import logutil
from db import insert_price
from runtime import Ctx, iso, parse_iso
from sources import PUMP_PROGRAM, RC_SRC, RateLimited, bonding_curve_address, top10_pct

WS_URL = "wss://pumpportal.fun/api/data"
PP_SRC = "pumpportal_migration.json"
log = logutil.get("scanner")
RECONNECT_ALERT_PER_HOUR = 10   # STEP 12: "More than 10 websocket reconnects in the last hour"


ON_MIGRATION_CAP_S = 60     # wall-clock cap on handling 1 migration message (RPC/Dex inside)
POLL_CAP_S = 120            # wall-clock cap on 1 waiting-room poll
ONCHAIN_CAP_S = 90          # wall-clock cap on 1 on-chain backup poll
ONCHAIN_DOWN_AFTER_S = 120  # PumpPortal not connected this long -> backup polls fast
ONCHAIN_MAX_TRIES = 5       # getTransaction failures before a signature is dropped
# pump.fun's migration authority signs every graduation (MigrateV2 onto PumpSwap).
MIGRATION_AUTHORITY = "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg"
WSOL = "So11111111111111111111111111111111111111112"

class QuietTimeout(Exception):
    """Our own idle timeout: no message for ws_silence_s, socket still fine."""


class ReconnectTracker:
    """Quiet reconnects (our idle timeout) are counted apart and never alert.
    Real disconnects (socket error, server close, failed connect) alert when
    more than RECONNECT_ALERT_PER_HOUR happen within the last hour."""

    def __init__(self, threshold: int = RECONNECT_ALERT_PER_HOUR):
        self.threshold = threshold
        self.quiet = 0
        self.real = 0
        self.real_times: list[float] = []
        self.last_alert: float | None = None

    def record(self, kind: str, now: float) -> bool:
        """kind 'quiet' | 'real'. Returns True when an alert should go out
        (at most 1 per hour)."""
        if kind == "quiet":
            self.quiet += 1
            return False
        self.real += 1
        self.real_times = [t for t in self.real_times if now - t < 3600] + [now]
        if len(self.real_times) > self.threshold and (
            self.last_alert is None or now - self.last_alert >= 3600
        ):
            self.last_alert = now
            return True
        return False

    def real_last_hour(self, now: float) -> int:
        return sum(1 for t in self.real_times if now - t < 3600)


FEED_DOWN_AFTER_S = 300   # no good open for 5 min while failing = "feed down" in the alert


def err_label(e: BaseException) -> str:
    """'ConnectionRefusedError', or 'InvalidStatus 502' when the server answered the
    websocket upgrade with an HTTP status (websockets' InvalidStatus carries .response)."""
    name = type(e).__name__
    code = getattr(getattr(e, "response", None), "status_code", None)
    return f"{name} {code}" if code is not None else name


def is_rejection(e: BaseException) -> bool:
    """PumpPortal turning us away: TCP refused, or an HTTP status instead of the upgrade
    (403/429 ban, 5xx their side). Kacper's cooldown applies to both (Nov 20 + Nov 23)."""
    return isinstance(e, ConnectionRefusedError) or getattr(getattr(e, "response", None), "status_code", None) is not None


def why_text(err: str | None) -> str:
    if not err:
        return "error"
    if err == "ConnectionRefusedError":
        return "connection refused"
    parts = err.split()
    if len(parts) == 2 and parts[1].isdigit():
        return f"PumpPortal answered HTTP {parts[1]}"
    return err


def backup_text(backup: int | None) -> str:
    """What happens to new coins while PumpPortal is down. backup = coins the on-chain
    backup feed brought in during the last hour, None = backup feed switched off."""
    if backup is None:
        return "no new coins until it reconnects"
    if backup == 0:
        return "backup on-chain feed is active (no new coins in the last hour)"
    return (f"backup on-chain feed is active ({backup} coin{'' if backup == 1 else 's'}"
            " in the last hour), coins still coming in")


def alert_text(n: int, down_since: float | None, err: str | None, now: float,
               backup: int | None = None) -> str:
    """'Websocket dropped {n} times in the last hour (PumpPortal)' (STEP 12 text, desk-health
    dedupes on that prefix) + when no connect has worked for 5 min: since when and why, and
    whether the on-chain backup feed is bringing coins in meanwhile."""
    text = f"Websocket dropped {n} times in the last hour (PumpPortal)"
    if down_since is not None and now - down_since >= FEED_DOWN_AFTER_S:
        from runtime import local_hhmm
        since = local_hhmm(iso(datetime.fromtimestamp(down_since, timezone.utc)))
        text += f" · feed down since {since} ({why_text(err)}), {backup_text(backup)}"
    return text


def migrate_mints(tx: dict | None) -> list[str]:
    """Graduated mint(s) in 1 migration-authority transaction: a successful tx whose logs
    show pump.fun's Migrate instruction; the mint is the non-WSOL token in its balances
    (LP / foreign mints are dropped later by is_pumpfun_coin)."""
    meta = (tx or {}).get("meta") or {}
    if meta.get("err") is not None:
        return []
    if not any("Instruction: Migrate" in ln for ln in meta.get("logMessages") or []):
        return []
    mints = []
    for b in meta.get("postTokenBalances") or []:
        m = b.get("mint")
        if m and m != WSOL and m not in mints:
            mints.append(m)
    pump = [m for m in mints if m.endswith("pump")]
    return pump or mints


# ------------------------------------------------------------------ pure helpers

def norm_name(s: str | None) -> str:
    """Lowercase, strip spaces, digits and emoji."""
    if not s:
        return ""
    out = []
    for ch in s.lower():
        if ch.isspace() or ch.isdigit():
            continue
        cat = unicodedata.category(ch)
        if cat in ("So", "Sk", "Cs", "Mn", "Cf", "Co"):  # emoji, modifiers, ZWJ, variation selectors
            continue
        out.append(ch)
    return "".join(out)


def _match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 3 and short in long_


def copycat_of(name: str | None, symbol: str | None, mint: str, runners: dict[str, dict]) -> str | None:
    """Runner label this coin copies, or None. name<->name, symbol<->symbol."""
    n, s = norm_name(name), norm_name(symbol)
    for rmint, r in runners.items():
        if rmint == mint:
            continue
        if _match(n, r["n"]) or _match(s, r["s"]):
            return r["label"]
    return None


def fmt_liq(x: float) -> str:
    if x >= 1_000_000:
        return f"${x / 1_000_000:.1f}M"
    if x < 1000:
        return f"${x:,.0f}"
    return f"${x / 1000:.1f}K"


def kill_reason(liq: float | None, top10: float | None, h1: float | None, copy: str | None,
                cfg: dict[str, Any]) -> str | None:
    """First kill that hits, in spec order (top10 may be None if not computed yet)."""
    if liq is None or liq < cfg["scanner.min_liquidity_usd"]:
        return "liq n/a" if liq is None else f"liq {fmt_liq(liq)}"
    if top10 is not None and top10 > cfg["scanner.max_top10_pct"]:
        return f"top 10 hold {top10:.0f}%"
    if h1 is not None and h1 > cfg["scanner.max_h1_change_pct"]:
        return f"+{h1:.0f}% in 1h"
    if copy:
        return f"copycat of {copy}"
    return None


# ------------------------------------------------------------------ agent

class Scanner:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.room: dict[str, dict] = {}        # mint -> {migratedAt, signature, creator_done, view}
        self.runners: dict[str, dict] = {}     # today's runners (h24 >= runner_h24_pct)
        self.runners_night: str | None = None
        self.reconnects = 0          # all reconnects (quiet + real)
        self.tracker = ReconnectTracker()
        self.migrations = 0
        self.ignored = 0
        self.last_open: float | None = None      # time.time() of the last good ws open
        self.down_since: float | None = None     # first failed connect since then
        self.last_error: str | None = None
        self.refused_streak = 0                   # connection refusals in a row (reset on open)
        self.cooldowns = 0
        self.pools_seen = 0
        # PumpPortal connection state for the on-chain backup feed
        import time as _t
        self.ws_connected = False
        self.ws_lost_at: float = _t.time()        # since when not connected
        # on-chain backup feed
        self.seen: dict[str, float] = {}          # mint -> time.time() first received (dedupe)
        self.onchain_cursor: str | None = None    # newest authority signature handled
        self.onchain_retry: dict[str, list] = {}  # sig -> [tries, blockTime] (getTransaction failed)
        self.onchain_times: list[float] = []      # time.time() of on-chain coins taken in
        self.onchain_polls = 0
        self.by_source = {"pumpportal": 0, "onchain": 0}
        self.dupes = 0

    # ---------- intake
    def restore_room(self) -> None:
        """After a restart, coins of this night still waiting go back in the room."""
        cfg = self.ctx.cfg()
        night = self.ctx.night_start()
        rows = self.ctx.conn.execute(
            "SELECT mint, migrated_at, creator FROM coins c WHERE migrated_at >= ? "
            "AND NOT EXISTS (SELECT 1 FROM scans s WHERE s.mint=c.mint)", (night,)
        ).fetchall()
        now = self.ctx.now()
        for m, mig, creator in rows:
            if (now - parse_iso(mig)).total_seconds() / 60 <= cfg["scanner.max_wait_min"]:
                self.room[m] = {"migratedAt": mig, "signature": None,
                                "creator_done": creator is not None, "view": None}

    async def on_migration(self, msg: dict, source: str = "pumpportal",
                           migrated_at: str | None = None) -> str | None:
        """1 migration from either feed, same message shape (PumpPortal keys).
        source 'pumpportal' | 'onchain'. migrated_at: block time when known (on-chain),
        else receive time. A mint already received by either feed is never taken twice."""
        mint = fields.get(msg, "mint", PP_SRC)
        if not mint:
            return None  # subscription ack / other message
        tx_type = fields.get(msg, "tx_type", PP_SRC)
        dex = fields.get(msg, "pumpportal_pool", PP_SRC)
        if tx_type != "migrate" or dex != "pump-amm":
            self.ignored += 1
            log.info(f"ignored\t{mint}\ttxType={tx_type}\tpool={dex}")
            return None
        conn = self.ctx.conn
        import time as _t
        now_t = _t.time()
        self.seen = {m: t for m, t in self.seen.items() if now_t - t < 86400}
        if mint in self.seen or mint in self.room or conn.execute(
            "SELECT 1 FROM scans WHERE mint=? AND ts>=?", (mint, self.ctx.night_start())
        ).fetchone() or conn.execute(
            "SELECT 1 FROM coins WHERE mint=? AND migrated_at IS NOT NULL", (mint,)
        ).fetchone():
            self.dupes += 1
            if mint not in self.seen:
                self.seen[mint] = now_t
            log.info(f"duplicate\t{mint}\tsource={source}\talready received")
            return None
        self.seen[mint] = now_t   # also for coins dropped below: no second curve check
        if not await self.is_pumpfun_coin(mint):
            self.ignored += 1
            log.info(f"ignored\t{mint}\tnot a pumpfun curve coin (pump-amm pool for another token)\tsource={source}")
            return None
        self.migrations += 1
        self.by_source[source] = self.by_source.get(source, 0) + 1
        if source == "onchain":
            self.onchain_times = [t for t in self.onchain_times if now_t - t < 3600] + [now_t]
        # PumpPortal has no migration time key (fields.py: migration_time = null) -> receive time.
        mig = migrated_at or self.ctx.now_iso()
        creator = fields.get(msg, "creator", PP_SRC)  # not in the PumpPortal sample -> None
        conn.execute(
            "INSERT INTO coins(mint, migrated_at, creator) VALUES (?,?,?) "
            "ON CONFLICT(mint) DO UPDATE SET migrated_at=excluded.migrated_at",
            (mint, mig, creator),
        )
        conn.commit()
        self.room[mint] = {"migratedAt": mig, "signature": fields.get(msg, "signature", PP_SRC),
                           "creator_done": False, "view": None}
        log.info(f"migration\t{mint}\tsig={self.room[mint]['signature']}\tsource={source}")
        return mint

    # ---------- on-chain backup feed
    def pumpportal_down(self, now: float) -> bool:
        return not self.ws_connected and now - self.ws_lost_at >= ONCHAIN_DOWN_AFTER_S

    def backup_last_hour(self, now: float) -> int | None:
        """Coins the on-chain feed took in during the last hour; None when it is off."""
        if self.ctx.cfg()["scanner.onchain_feed"] != "on":
            return None
        return sum(1 for t in self.onchain_times if now - t < 3600)

    async def onchain_poll_once(self) -> int:
        """1 poll of the migration authority. First poll: the last max_wait_min of history
        (coins that can still be decided). Returns coins taken in."""
        rpc = self.ctx.src.rpc
        self.onchain_polls += 1
        opts: dict[str, Any] = {"limit": 100}
        if self.onchain_cursor:
            opts["until"] = self.onchain_cursor
        page = await rpc.call("getSignaturesForAddress", [MIGRATION_AUTHORITY, opts], timeout=15) or []
        if not self.onchain_cursor:
            horizon = self.ctx.now().timestamp() - self.ctx.cfg()["scanner.max_wait_min"] * 60
            page = [r for r in page if (r.get("blockTime") or 0) >= horizon]
            if not page:
                first = await rpc.call("getSignaturesForAddress", [MIGRATION_AUTHORITY, {"limit": 1}], timeout=15) or []
                if first:
                    self.onchain_cursor = first[0]["signature"]
                    log.info("onchain_start\tno migration in the last max_wait_min")
                return 0
        todo: list[tuple[str, int | None]] = [(sig, v[1]) for sig, v in self.onchain_retry.items()]
        for r in reversed(page):   # oldest first
            if r.get("err") is None and r["signature"] not in self.onchain_retry:
                todo.append((r["signature"], r.get("blockTime")))
        if page:
            start = self.onchain_cursor is None
            self.onchain_cursor = page[0]["signature"]
            if start:
                log.info(f"onchain_start\tbackfill {len(todo)} authority tx of the last "
                         f"{self.ctx.cfg()['scanner.max_wait_min']} min")
        taken = 0
        for sig, bt in todo:
            try:
                tx = await rpc.call("getTransaction", [sig, {"encoding": "json", "commitment": "confirmed",
                                                             "maxSupportedTransactionVersion": 1}], timeout=15)
                if tx is None:
                    raise ValueError("not yet available")
            except RateLimited:
                self.onchain_retry.setdefault(sig, [0, bt])
                raise
            except Exception as e:  # noqa: BLE001
                ent = self.onchain_retry.setdefault(sig, [0, bt])
                ent[0] += 1
                if ent[0] >= ONCHAIN_MAX_TRIES:
                    del self.onchain_retry[sig]
                    log.info(f"onchain_tx_dropped\t{sig[:16]}\t{type(e).__name__}: {e}")
                continue
            self.onchain_retry.pop(sig, None)
            bt = tx.get("blockTime") or bt
            mig = iso(datetime.fromtimestamp(bt, timezone.utc)) if bt else None
            for mint in migrate_mints(tx):
                msg = {"signature": sig, "mint": mint, "txType": "migrate", "pool": "pump-amm"}
                try:
                    got = await asyncio.wait_for(self.on_migration(msg, source="onchain", migrated_at=mig),
                                                 ON_MIGRATION_CAP_S)
                except asyncio.TimeoutError:
                    log.info(f"on_migration_timeout\t{ON_MIGRATION_CAP_S}s\t{mint}\tsource=onchain")
                    continue
                if got:
                    taken += 1
                    break   # 1 graduated coin per migrate tx
        return taken

    async def onchain_loop(self) -> None:
        """Backup feed, always on as a dedupe safety net: every onchain_poll_s while
        PumpPortal has been down 2+ min (or in its cooldown), else every onchain_safety_poll_s."""
        import time as _t
        while True:
            cfg = self.ctx.cfg()
            if cfg["scanner.onchain_feed"] == "on" and not bus.is_paused("scanner"):
                try:
                    await asyncio.wait_for(self.onchain_poll_once(), ONCHAIN_CAP_S)
                except asyncio.TimeoutError:
                    log.info(f"onchain_timeout\t{ONCHAIN_CAP_S}s")
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001  RPC down / 429: next poll tries again
                    log.info(f"onchain_error\t{type(e).__name__}: {str(e)[:120]}")
            bus.beat("scanner")
            down = self.pumpportal_down(_t.time())
            await asyncio.sleep(cfg["scanner.onchain_poll_s"] if down else cfg["scanner.onchain_safety_poll_s"])

    async def is_pumpfun_coin(self, mint: str) -> bool:
        """OWN: "every coin that graduates off the pumpfun curve ... nothing else".
        PumpPortal also reports migrate/pump-amm for pools other tokens open on PumpSwap
        (43 of 513 on Nov 19-20, incl. the PUMP token itself, whose millions of holders
        OOM-killed the desk). Pumpfun mints end in 'pump'; any other mint must have a
        pumpfun bonding-curve account. RPC down -> let it in (the size caps still hold)."""
        if mint.endswith("pump"):
            return True
        try:
            curve = bonding_curve_address(mint)
        except Exception:  # noqa: BLE001  not a valid base58 key -> not a coin at all
            return False
        try:
            info = await self.ctx.src.rpc.account_info(curve)
        except Exception as e:  # noqa: BLE001
            log.info(f"curve_check_failed\t{mint}\t{type(e).__name__}")
            return True
        value = ((info or {}).get("result") or {}).get("value")
        return bool(value) and value.get("owner") == PUMP_PROGRAM

    async def fill_creator(self, mint: str) -> None:
        """Creator from RugCheck (PumpPortal has none) + its balance NOW."""
        entry = self.room.get(mint)
        if entry is None or entry["creator_done"]:
            return
        conn = self.ctx.conn
        creator = conn.execute("SELECT creator FROM coins WHERE mint=?", (mint,)).fetchone()[0]
        try:
            if creator is None:
                report = await self.ctx.src.rugcheck.report(mint)
                creator = fields.get(report, "creator", RC_SRC)
            if creator is None:
                return  # retried on the next poll while the coin waits
            bal = await self.ctx.src.rpc.owner_balance(creator, mint)
        except Exception as e:  # noqa: BLE001
            log.info(f"creator_lookup_failed\t{mint}\t{type(e).__name__}")
            return
        conn.execute("UPDATE coins SET creator=?, creator_balance_at_migration=? WHERE mint=?",
                     (creator, bal, mint))
        conn.commit()
        entry["creator_done"] = True

    # ---------- polling
    def _track_runners(self, mint: str, v: dict, cfg: dict) -> None:
        night = self.ctx.night_start()
        if self.runners_night != night:
            self.runners, self.runners_night = {}, night
        h24 = v.get("changeH24")
        if h24 is not None and h24 >= cfg["scanner.runner_h24_pct"]:
            self.runners[mint] = {"n": norm_name(v.get("name")), "s": norm_name(v.get("symbol")),
                                  "label": v.get("name") or v.get("symbol") or mint}

    async def poll_once(self) -> list[dict]:
        cfg = self.ctx.cfg()
        conn = self.ctx.conn
        bus.beat("scanner")
        if not self.room:
            return []
        mints = list(self.room)
        views = await self.ctx.src.dex.tokens(mints)
        now = self.ctx.now()
        ts = iso(now)
        for m, v in views.items():
            insert_price(conn, "scanner", m, ts, v.get("priceUsd"), v.get("volumeM5"))
            conn.execute(
                "UPDATE coins SET name=COALESCE(?, name), symbol=COALESCE(?, symbol), "
                "pool=COALESCE(?, pool) WHERE mint=?",
                (v.get("name"), v.get("symbol"), v.get("pairAddress"), m),
            )
            if self.room[m]["view"] is None:
                self.pools_seen += 1
            self.room[m]["view"] = v
            self._track_runners(m, v, cfg)
        conn.commit()
        decisions = []
        for m in mints:
            if not self.room[m]["creator_done"]:
                await self.fill_creator(m)
            d = await self.decide(m, cfg, now)
            if d:
                decisions.append(d)
        return decisions

    async def decide(self, mint: str, cfg: dict, now) -> dict | None:
        entry = self.room[mint]
        age_min = (now - parse_iso(entry["migratedAt"])).total_seconds() / 60
        v = entry["view"]
        if v is None:
            # DexScreener doesn't know it yet: it waits in the room.
            if age_min >= cfg["scanner.max_wait_min"]:
                return await self.emit(mint, None, None, None, age_min, "kill", "never qualified")
            return None
        if age_min < cfg["scanner.min_pool_age_min"]:
            if age_min >= cfg["scanner.max_wait_min"]:
                return await self.emit(mint, v, None, None, age_min, "kill", "never qualified")
            return None
        liq, h1 = v.get("liquidityUsd"), v.get("changeH1")
        top10 = None
        reason = kill_reason(liq, None, None, None, cfg)
        if reason is None:
            try:
                supply, _ = await self.ctx.src.rpc.token_supply(mint)
                excl = {x for x in (v.get("pairAddress"), bonding_curve_address(mint)) if x}
                top10 = await top10_pct(self.ctx.src.rpc, mint, excl, supply, log)
            except Exception as e:  # noqa: BLE001
                log.info(f"top10_failed\t{mint}\t{type(e).__name__}")
                if age_min >= cfg["scanner.max_wait_min"]:
                    return await self.emit(mint, v, None, h1, age_min, "kill", "never qualified")
                return None  # no number yet: wait, never pass without it
            copy = copycat_of(v.get("name"), v.get("symbol"), mint, self.runners)
            reason = kill_reason(liq, top10, h1, copy, cfg)
        verdict = "kill" if reason else "pass"
        return await self.emit(mint, v, top10, h1, age_min, verdict, reason)

    async def emit(self, mint, v, top10, h1, age_min, verdict, reason) -> dict:
        conn = self.ctx.conn
        v = v or {}
        out = {
            "agent": "scanner", "mint": mint, "name": v.get("name"), "symbol": v.get("symbol"),
            "pool": v.get("pairAddress"), "liquidityUsd": v.get("liquidityUsd"),
            "top10Pct": None if top10 is None else round(top10, 2),
            "h1ChangePct": h1, "ageMin": int(age_min), "verdict": verdict, "reason": reason,
            "ts": self.ctx.now_iso(),
        }
        if verdict == "pass" and conn.execute(
            "SELECT 1 FROM scans WHERE mint=? AND verdict='pass' AND ts>=?", (mint, self.ctx.night_start())
        ).fetchone():
            self.room.pop(mint, None)
            return None  # 1 coin passes once a night
        conn.execute(
            "INSERT INTO scans(mint, ts, liquidity_usd, top10_pct, h1_change_pct, age_min, verdict, reason)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (mint, out["ts"], out["liquidityUsd"], out["top10Pct"], h1, out["ageMin"], verdict, reason),
        )
        conn.commit()
        self.room.pop(mint, None)
        log.info("\t".join(str(x) for x in (out["ts"], mint, verdict, reason, out["liquidityUsd"],
                                             out["top10Pct"], h1, out["ageMin"])))
        bus.publish("scan", out)
        if verdict == "pass":
            await bus.scanner_to_auditor.put(out)
        return out

    # ---------- loops
    async def on_reconnect(self, kind: str, err: str, backoff: float) -> bool:
        """Count a reconnect. kind 'quiet' (our idle timeout) or 'real'. Returns True if alerted."""
        import time as _t

        now = _t.time()
        if self.ws_connected:
            self.ws_connected, self.ws_lost_at = False, now
        self.reconnects += 1
        alert = self.tracker.record(kind, now)
        if kind == "quiet":
            log.info(f"ws_quiet_reconnect\t{self.tracker.quiet}\tidle {self.ctx.cfg()['scanner.ws_silence_s']}s")
        else:
            n = self.tracker.real_last_hour(now)
            if self.down_since is None:
                self.down_since = now
            self.last_error = err
            log.info(f"ws_disconnect\t{self.tracker.real}\t{err}\tbackoff={backoff}s\tlast_hour={n}")
        if alert:
            n = self.tracker.real_last_hour(now)
            await self.ctx.notifier.message(alert_text(n, self.down_since, self.last_error, now,
                                                       self.backup_last_hour(now)), kind="alert")
        return alert

    def on_open(self) -> None:
        import time as _t
        self.last_open, self.down_since, self.last_error = _t.time(), None, None
        self.refused_streak = 0
        self.ws_connected = True
        log.info("ws_open")

    def next_wait(self, err: BaseException, backoff: float) -> float:
        """Seconds to wait before the next connect. Normal: the backoff (1, 2, 4 ... 60).
        Kacper (Nov 20, extended Nov 23): after [10] rejections in a row (TCP refused, or an
        HTTP status such as 502/403/429 instead of the upgrade) wait [15] min once, then the
        normal backoff again; a good open resets the count."""
        if not is_rejection(err):
            self.refused_streak = 0
            return backoff
        self.refused_streak += 1
        cfg = self.ctx.cfg()
        if self.refused_streak >= cfg["scanner.refused_cooldown_after"]:
            n, self.refused_streak = self.refused_streak, 0
            self.cooldowns += 1
            wait = cfg["scanner.refused_cooldown_min"] * 60
            log.info(f"ws_cooldown\t{n} rejections in a row ({err_label(err)})\t"
                     f"wait {cfg['scanner.refused_cooldown_min']} min\tcooldown={self.cooldowns}")
            return wait
        return backoff

    async def ws_loop(self) -> None:
        import websockets

        backoff = 1
        while True:
            try:
                async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20,
                                              open_timeout=20, close_timeout=5, max_size=2**20) as ws:
                    # every await on the socket has a wall-clock cap (a dead socket can't freeze us)
                    await asyncio.wait_for(ws.send(json.dumps({"method": "subscribeMigration"})), 10)
                    self.on_open()
                    backoff = 1  # a good open resets the backoff
                    silence = self.ctx.cfg()["scanner.ws_silence_s"]
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=silence)
                        except asyncio.TimeoutError:
                            raise QuietTimeout() from None
                        bus.beat("scanner")
                        try:
                            msg = json.loads(raw)
                        except ValueError:
                            continue
                        if isinstance(msg, dict):
                            try:
                                await asyncio.wait_for(self.on_migration(msg), ON_MIGRATION_CAP_S)
                            except asyncio.TimeoutError:
                                log.info(f"on_migration_timeout\t{ON_MIGRATION_CAP_S}s\t{str(msg.get('mint'))[:44]}")
            except asyncio.CancelledError:
                raise
            except QuietTimeout:
                # spec: silent for [120]s -> reconnect. Not a real disconnect, no backoff growth.
                await self.on_reconnect("quiet", "idle", 0)
                continue
            except Exception as e:  # noqa: BLE001  socket error, server close, failed connect
                await self.on_reconnect("real", err_label(e), backoff)
                await asyncio.sleep(self.next_wait(e, backoff))
                backoff = min(backoff * 2, 60)

    async def poll_loop(self) -> None:
        while True:
            cfg = self.ctx.cfg()
            if not bus.is_paused("scanner"):
                try:
                    await asyncio.wait_for(self.poll_once(), POLL_CAP_S)
                except asyncio.TimeoutError:
                    log.info(f"poll_timeout\t{POLL_CAP_S}s\troom={len(self.room)}")
                except RateLimited:
                    log.info(f"dexscreener_429\twait {cfg['scanner.dex_429_wait_s']}s\troom={len(self.room)}")
                    await asyncio.sleep(cfg["scanner.dex_429_wait_s"])
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    log.info(f"poll_error\t{type(e).__name__}: {e}")
            bus.beat("scanner")
            await asyncio.sleep(cfg["scanner.poll_s"])

    async def run(self) -> None:
        self.restore_room()
        await asyncio.gather(self.ws_loop(), self.poll_loop(), self.onchain_loop())


async def run(ctx: Ctx) -> None:
    await Scanner(ctx).run()
