"""Shared handles the agents get from main.py (db connection, source clients,
notifier, clock). Agents still exchange data only through bus.py and the db."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import config


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@dataclass
class Ctx:
    conn: sqlite3.Connection
    src: Any                     # sources.Sources or a test fake
    notifier: Any                # notifier.Notifier
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))
    fast: bool = False           # tests: no real sleeps between retries
    night_hook: Any = None       # async () -> None: STEP 10/11 ticket check before the first coin of a night

    def now(self) -> datetime:
        return self.clock()

    def now_iso(self) -> str:
        return iso(self.clock())

    def cfg(self) -> dict[str, Any]:
        return config.load(self.conn)

    def night_start(self) -> str:
        return config.night_start(self.cfg(), self.now())


def local_hhmm(ts_iso: str) -> str:
    """'6:52 PM' in the owner's zone (America/New_York)."""
    return parse_iso(ts_iso).astimezone(config.LOCAL_TZ).strftime("%-I:%M %p")


def money(x: float) -> str:
    return f"{abs(x):,.2f}"


def pct_txt(x: float) -> str:
    return f"{x:g}"


def coin_label(conn: sqlite3.Connection, mint: str) -> str:
    row = conn.execute("SELECT name, symbol FROM coins WHERE mint=?", (mint,)).fetchone()
    if row and (row[0] or row[1]):
        return row[0] or row[1]
    return mint[:4] + "..." + mint[-3:]


def short_addr(a: str) -> str:
    return f"{a[:4]}...{a[-3:]}" if a and len(a) > 9 else (a or "")


