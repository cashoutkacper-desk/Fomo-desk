"""STEP 12 · desk-health (server side of the hourly routine).

Run on the server as user desk (wrapper /usr/local/bin/desk-health):
  1. desk.service active? every agent heartbeat (+ 'process') younger than 2 min?
     If not: restart desk.service, wait for fresh heartbeats, and tell the owner
     "Restarted {agent}, down {n} min" (one per stale agent, or 'desk' for the process).
  2. Real websocket reconnects (ws_disconnect, NOT ws_quiet_reconnect) in the last
     60 min from logs/scanner.log*. More than 10 -> one Telegram alert, unless the
     same alert already went out this hour (from main.py or an earlier run).
Prints one JSON line: {ok, restarted, down_min, reconnects_1h, alerted}.
Exit 0 = handled (healthy, or restarted and fresh again); 1 = still unhealthy.
Never prints secrets; Telegram only to the owner chat (notifier).
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

AGENTS = ("scanner", "auditor", "analyst", "executor", "risk")
STALE_S = 120
RECONNECT_ALERT_PER_HOUR = 10        # same threshold as agents/scanner.py
ALERT_PREFIX = "Websocket dropped "  # same text main.py's scanner alert uses
SERVICE = "desk.service"


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def heartbeat_ages(conn: sqlite3.Connection, now: datetime) -> dict[str, float | None]:
    """agent -> age in seconds (None = no heartbeat row at all)."""
    rows = {r[0]: r[1] for r in conn.execute("SELECT agent, ts FROM heartbeats")}
    out: dict[str, float | None] = {}
    for a in ("process", *AGENTS):
        ts = rows.get(a)
        out[a] = None if not ts else max(0.0, (now - _parse_iso(ts)).total_seconds())
    return out


def down_minutes(age_s: float | None) -> int:
    return 0 if age_s is None else int(round(age_s / 60))


def stale_list(ages: dict[str, float | None], service_active: bool) -> list[tuple[str, int]]:
    """-> [(name, down_min)]: [('desk', n)] if the process is down, else one per stale agent."""
    p = ages.get("process")
    if not service_active or p is None or p > STALE_S:
        return [("desk", down_minutes(p))]
    return [(a, down_minutes(ages[a])) for a in AGENTS if ages[a] is None or ages[a] > STALE_S]


def _log_lines(log_dir: Path, name: str) -> list[str]:
    files = sorted(log_dir.glob(f"{name}.log.*")) + [log_dir / f"{name}.log"]
    lines: list[str] = []
    for f in files[-2:]:                       # today + yesterday's rotation covers 60 min
        try:
            lines += f.read_text(encoding="utf-8", errors="replace").splitlines()
        except FileNotFoundError:
            pass
    return lines


_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def count_reconnects(log_dir: Path, now_local: datetime, window_s: int = 3600) -> int:
    """Real reconnects only: lines '<asctime>\\tws_disconnect\\t...'. Quiet ones
    (ws_quiet_reconnect = our idle timeout) never count (Step 5 decision).
    Log times are the server's local wall clock (naive)."""
    since = now_local - timedelta(seconds=window_s)
    n = 0
    for ln in _log_lines(log_dir, "scanner"):
        parts = ln.split("\t")
        if len(parts) < 2 or parts[1] != "ws_disconnect":
            continue
        m = _TS.match(ln)
        if m and since <= datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S") <= now_local:
            n += 1
    return n


def alert_sent_this_hour(log_dir: Path, now: datetime) -> bool:
    """True if a 'Websocket dropped ...' message was delivered in the last 60 min
    (by main.py's scanner or an earlier desk-health run): both log to notifier.log."""
    since = now - timedelta(hours=1)
    for ln in _log_lines(log_dir, "notifier"):
        i = ln.find("\t{")
        if i < 0:
            continue
        try:
            rec = json.loads(ln[i + 1:])
        except ValueError:
            continue
        if str(rec.get("text", "")).startswith(ALERT_PREFIX) and rec.get("ts"):
            try:
                if _parse_iso(rec["ts"]) >= since:
                    return True
            except ValueError:
                continue
    return False


def service_active() -> bool:
    r = subprocess.run(["systemctl", "is-active", "--quiet", SERVICE], timeout=20)
    return r.returncode == 0


def restart_service() -> bool:
    # desk may run exactly this via sudoers (/etc/sudoers.d/desk-health); nothing else
    r = subprocess.run(["sudo", "-n", "/usr/bin/systemctl", "restart", SERVICE],
                       timeout=60, capture_output=True)
    return r.returncode == 0


async def run(conn: sqlite3.Connection, notifier: Any, log_dir: Path, *,
              now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
              now_local: Callable[[], datetime] = lambda: datetime.now(),
              is_active: Callable[[], bool] = service_active,
              restart: Callable[[], bool] = restart_service,
              sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
              wait_s: float = 90, step_s: float = 5) -> dict:
    t0 = now()
    stale = stale_list(heartbeat_ages(conn, t0), is_active())
    restarted: list[str] = []
    fresh = True
    if stale:
        ok = restart()
        restarted = [name for name, _ in stale]
        fresh = False
        if ok:
            waited = 0.0
            while waited <= wait_s:
                ages = heartbeat_ages(conn, now())
                hb = {a: conn.execute("SELECT ts FROM heartbeats WHERE agent=?", (a,)).fetchone() for a in ages}
                if all(r and _parse_iso(r[0]) > t0 for r in hb.values()):
                    fresh = True
                    break
                await sleep(step_s)
                waited += step_s
        for name, n in stale:
            await notifier.message(f"Restarted {name}, down {n} min", kind="health")
    n_rec = count_reconnects(log_dir, now_local())
    alerted = False
    if n_rec > RECONNECT_ALERT_PER_HOUR and not alert_sent_this_hour(log_dir, now()):
        alerted = await notifier.message(f"{ALERT_PREFIX}{n_rec} times in the last hour (PumpPortal)",
                                         kind="alert")
    return {"ok": fresh, "restarted": restarted, "down_min": max([n for _, n in stale], default=0),
            "reconnects_1h": n_rec, "alerted": alerted}


def main() -> int:
    import httpx

    import config
    import db
    import notifier as notifier_mod

    conn = db.connect()
    cfg = config.load(conn)

    async def go():
        async with httpx.AsyncClient() as client:
            return await run(conn, notifier_mod.build(cfg, client), config.LOG_DIR)
    try:
        res = asyncio.run(go())
    except Exception as e:  # noqa: BLE001  (type only: never echo URLs/tokens)
        print(json.dumps({"ok": False, "error": type(e).__name__}))
        return 2
    print(json.dumps(res))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
