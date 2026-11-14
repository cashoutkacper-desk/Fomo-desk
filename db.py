"""SQLite schema (STEP 4) and helpers.

Connections use WAL mode and have foreign_keys enabled.
Secrets are never read or logged here.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import DB_PATH, DESK_ROOT
from links import coin_url

BOOTSTRAP_PATH = DESK_ROOT / "bootstrap-settings.json"
BOOTSTRAP_KEYS = ("fomo_handle", "analyst_mode", "owner_chat_id")
PRICE_WRITERS = ("scanner", "risk", "api")

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS coins (
        mint TEXT PRIMARY KEY,
        name TEXT,
        symbol TEXT,
        pool TEXT,
        migrated_at TEXT,
        creator TEXT,
        creator_balance_at_migration REAL,
        holders INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mint TEXT NOT NULL REFERENCES coins(mint),
        ts TEXT NOT NULL,
        liquidity_usd REAL,
        top10_pct REAL,
        h1_change_pct REAL,
        age_min REAL,
        verdict TEXT,
        reason TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mint TEXT NOT NULL REFERENCES coins(mint),
        ts TEXT NOT NULL,
        layer INTEGER,
        name TEXT,
        value_text TEXT,
        pass INTEGER,
        source TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS verdicts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mint TEXT NOT NULL REFERENCES coins(mint),
        ts TEXT NOT NULL,
        alive_6h INTEGER,
        organic INTEGER,
        flush INTEGER,
        chase INTEGER,
        score REAL,
        pass INTEGER,
        reason TEXT,
        input_tokens INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mint TEXT NOT NULL REFERENCES coins(mint),
        ts TEXT NOT NULL,
        price REAL,
        size_usd REAL,
        stack_pct REAL,
        link TEXT,
        mode TEXT CHECK (mode IN ('PAPER', 'LIVE')),
        status TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mint TEXT NOT NULL REFERENCES coins(mint),
        opened_at TEXT NOT NULL,
        entry_price REAL,
        size_usd REAL,
        peak_price REAL,
        stop_price REAL,
        trail_active INTEGER,
        closed_at TEXT,
        exit_price REAL,
        exit_reason TEXT CHECK (
            exit_reason IN ('stop', 'trail', 'timeout', 'pulled')
            OR exit_reason IS NULL
        ),
        pnl_usd REAL,
        fees_usd REAL,
        mode TEXT CHECK (mode IN ('PAPER', 'LIVE'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS activity (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('bought', 'sold', 'stopped', 'rejected')),
        mint TEXT REFERENCES coins(mint),
        agent TEXT,
        title TEXT,
        subtitle TEXT,
        value TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS prices (
        mint TEXT NOT NULL REFERENCES coins(mint),
        ts TEXT NOT NULL,
        price REAL,
        volume_m5 REAL,
        writer TEXT NOT NULL CHECK (writer IN ('scanner', 'risk', 'api'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS balance (
        ts TEXT NOT NULL,
        value REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS change_log (
        ts TEXT NOT NULL,
        key TEXT NOT NULL,
        old TEXT,
        new TEXT,
        who TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS skips (
        mint TEXT PRIMARY KEY REFERENCES coins(mint),
        until TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS heartbeats (
        agent TEXT PRIMARY KEY,
        ts TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS push_subs (
        endpoint TEXT PRIMARY KEY,
        p256dh TEXT,
        auth TEXT,
        created_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_prices_mint_ts ON prices(mint, ts)",
    "CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity(ts)",
    "CREATE INDEX IF NOT EXISTS idx_audits_mint ON audits(mint)",
    "CREATE INDEX IF NOT EXISTS idx_scans_mint ON scans(mint)",
    """
    CREATE INDEX IF NOT EXISTS idx_positions_open
    ON positions(mint)
    WHERE closed_at IS NULL
    """,
)

TABLES = (
    "coins",
    "scans",
    "audits",
    "verdicts",
    "signals",
    "positions",
    "activity",
    "prices",
    "balance",
    "settings",
    "change_log",
    "skips",
    "heartbeats",
    "push_subs",
)


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp with a Z suffix."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _as_setting_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _resolve_path(path: str | Path | None = None) -> Path:
    return Path(path) if path is not None else Path(DB_PATH)


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open a WAL-mode SQLite connection with foreign_keys on."""
    db_path = _resolve_path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # one asyncio loop owns a connection; check_same_thread off so the API test client
    # (its own thread) and asyncio.to_thread helpers can use it too.
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path: str | Path | None = None) -> Path:
    """Create all tables idempotently and seed missing bootstrap settings."""
    db_path = _resolve_path(path)
    conn = connect(db_path)
    try:
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)
        _seed_settings(conn)
        conn.commit()
    finally:
        conn.close()
    return db_path


def init_schema(path: str | Path | None = None) -> Path:
    """Alias for init_db, kept from the STEP 3 stub name."""
    return init_db(path)


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    return row["value"]


def set_setting(conn: sqlite3.Connection, key: str, value: Any, who: str) -> None:
    """Upsert a setting and append a change_log row."""
    ts = utc_now()
    old = get_setting(conn, key)
    new = _as_setting_text(value)
    conn.execute(
        """
        INSERT INTO settings(key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (key, new, ts),
    )
    conn.execute(
        "INSERT INTO change_log(ts, key, old, new, who) VALUES (?, ?, ?, ?, ?)",
        (ts, key, old, new, who),
    )


def insert_price(
    conn: sqlite3.Connection,
    writer: str,
    mint: str,
    ts: str | None = None,
    price: float | None = None,
    volume_m5: float | None = None,
) -> None:
    """Insert a DexScreener price tick attributed to one writer."""
    if writer not in PRICE_WRITERS:
        raise ValueError(f"writer must be one of {PRICE_WRITERS}, got {writer!r}")
    conn.execute(
        """
        INSERT INTO prices(mint, ts, price, volume_m5, writer)
        VALUES (?, ?, ?, ?, ?)
        """,
        (mint, ts or utc_now(), price, volume_m5, writer),
    )


def insert_signal(
    conn: sqlite3.Connection,
    mint: str,
    ts: str | None = None,
    price: float | None = None,
    size_usd: float | None = None,
    stack_pct: float | None = None,
    mode: str | None = None,
    status: str | None = None,
    chain: str = "solana",
) -> int:
    """Insert a signal whose link is built only by links.coin_url."""
    link = coin_url(mint, chain)
    cur = conn.execute(
        """
        INSERT INTO signals(mint, ts, price, size_usd, stack_pct, link, mode, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (mint, ts or utc_now(), price, size_usd, stack_pct, link, mode, status),
    )
    return int(cur.lastrowid)


def _seed_settings(conn: sqlite3.Connection) -> None:
    if not BOOTSTRAP_PATH.is_file():
        return
    payload = json.loads(BOOTSTRAP_PATH.read_text())
    settings = payload.get("settings") or {}
    ts = utc_now()
    for key in BOOTSTRAP_KEYS:
        if key not in settings:
            continue
        exists = conn.execute(
            "SELECT 1 FROM settings WHERE key = ?", (key,)
        ).fetchone()
        if exists is not None:
            continue
        conn.execute(
            "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)",
            (key, _as_setting_text(settings[key]), ts),
        )
