"""STEP 4 SQLite schema and helper tests. Uses a temporary database only."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import db
from links import coin_url

EXPECTED_TABLES = (
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

EXPECTED_COLUMNS = {
    "coins": [
        "mint",
        "name",
        "symbol",
        "pool",
        "migrated_at",
        "creator",
        "creator_balance_at_migration",
        "holders",
    ],
    "scans": [
        "id",
        "mint",
        "ts",
        "liquidity_usd",
        "top10_pct",
        "h1_change_pct",
        "age_min",
        "verdict",
        "reason",
    ],
    "audits": ["id", "mint", "ts", "layer", "name", "value_text", "pass", "source"],
    "verdicts": [
        "id",
        "mint",
        "ts",
        "alive_6h",
        "organic",
        "flush",
        "chase",
        "score",
        "pass",
        "reason",
        "input_tokens",
    ],
    "signals": [
        "id",
        "mint",
        "ts",
        "price",
        "size_usd",
        "stack_pct",
        "link",
        "mode",
        "status",
    ],
    "positions": [
        "id",
        "mint",
        "opened_at",
        "entry_price",
        "size_usd",
        "peak_price",
        "stop_price",
        "trail_active",
        "closed_at",
        "exit_price",
        "exit_reason",
        "pnl_usd",
        "fees_usd",
        "mode",
    ],
    "activity": ["id", "ts", "kind", "mint", "agent", "title", "subtitle", "value"],
    "prices": ["mint", "ts", "price", "volume_m5", "writer"],
    "balance": ["ts", "value"],
    "settings": ["key", "value", "updated_at"],
    "change_log": ["ts", "key", "old", "new", "who"],
    "skips": ["mint", "until"],
    "heartbeats": ["agent", "ts"],
    "push_subs": ["endpoint", "p256dh", "auth", "created_at"],
}

SAMPLE_MINT = "4Ye6Y6Vaic8cyPdfW2f9PcLhXkysBtSmV84G8X7spump"


@pytest.fixture
def desk_db(tmp_path: Path):
    path = tmp_path / "desk.db"
    db.init_db(path)
    conn = db.connect(path)
    try:
        yield path, conn
    finally:
        conn.close()


def _table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [row["name"] for row in rows]


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def test_init_db_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "desk.db"
    db.init_db(path)
    db.init_db(path)
    conn = db.connect(path)
    try:
        assert set(_table_names(conn)) == set(EXPECTED_TABLES)
        assert len(_table_names(conn)) == len(EXPECTED_TABLES)
    finally:
        conn.close()


def test_journal_mode_wal(desk_db) -> None:
    _path, conn = desk_db
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_foreign_keys_on(desk_db) -> None:
    _path, conn = desk_db
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_table_columns(desk_db) -> None:
    _path, conn = desk_db
    assert _table_names(conn) == sorted(EXPECTED_TABLES)
    for table, columns in EXPECTED_COLUMNS.items():
        assert _columns(conn, table) == columns, table


def test_required_indexes(desk_db) -> None:
    _path, conn = desk_db
    index_sql = [
        row["sql"] or ""
        for row in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
        ).fetchall()
    ]
    blob = "\n".join(index_sql)
    assert "ON prices(mint, ts)" in blob
    assert "ON activity(ts)" in blob
    assert "ON audits(mint)" in blob
    assert "ON scans(mint)" in blob
    assert "ON positions(mint)" in blob
    assert "closed_at IS NULL" in blob


def test_bootstrap_settings_seeded_only_if_absent(tmp_path: Path) -> None:
    path = tmp_path / "desk.db"
    db.init_db(path)
    conn = db.connect(path)
    try:
        assert db.get_setting(conn, "fomo_handle") == "Cashoutkacper"
        assert db.get_setting(conn, "analyst_mode") == "jev"
        assert db.get_setting(conn, "owner_chat_id") == "6307862350"
        conn.execute(
            "UPDATE settings SET value = 'kept' WHERE key = 'fomo_handle'"
        )
        conn.commit()
    finally:
        conn.close()

    db.init_db(path)
    conn = db.connect(path)
    try:
        assert db.get_setting(conn, "fomo_handle") == "kept"
        log_count = conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0]
        assert log_count == 0
    finally:
        conn.close()


def test_smoke_inserts_and_signal_link(desk_db) -> None:
    _path, conn = desk_db
    ts = "2026-11-13T22:00:00.000Z"
    conn.execute(
        """
        INSERT INTO coins(mint, name, symbol, pool, migrated_at, creator, holders)
        VALUES (?, 'Sample', 'SAMP', 'pump-amm', ?, 'creator111', 100)
        """,
        (SAMPLE_MINT, ts),
    )
    db.insert_price(conn, "scanner", SAMPLE_MINT, ts, 0.00123, 42.5)
    conn.execute(
        """
        INSERT INTO activity(ts, kind, mint, agent, title, subtitle, value)
        VALUES (?, 'bought', ?, 'executor', 'Bought SAMP', 'paper', '10')
        """,
        (ts, SAMPLE_MINT),
    )
    signal_id = db.insert_signal(
        conn,
        mint=SAMPLE_MINT,
        ts=ts,
        price=0.00123,
        size_usd=10.0,
        stack_pct=2.0,
        mode="PAPER",
        status="pending",
        chain="solana",
    )
    conn.commit()

    price = conn.execute("SELECT * FROM prices").fetchone()
    assert price["writer"] == "scanner"
    assert price["price"] == 0.00123
    assert price["volume_m5"] == 42.5

    activity = conn.execute("SELECT * FROM activity").fetchone()
    assert activity["kind"] == "bought"

    signal = conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
    expected = coin_url(SAMPLE_MINT, "solana")
    assert signal["link"] == expected
    assert "r=savipww" in signal["link"]
    assert "chainId=1399811149" in signal["link"]


def test_set_setting_writes_change_log(desk_db) -> None:
    _path, conn = desk_db
    old = db.get_setting(conn, "analyst_mode")
    assert old == "jev"
    db.set_setting(conn, "analyst_mode", "rules", "owner")
    conn.commit()
    assert db.get_setting(conn, "analyst_mode") == "rules"
    row = conn.execute(
        "SELECT key, old, new, who FROM change_log WHERE key = 'analyst_mode'"
    ).fetchone()
    assert row["old"] == "jev"
    assert row["new"] == "rules"
    assert row["who"] == "owner"


def test_activity_kind_rejects_foo(desk_db) -> None:
    _path, conn = desk_db
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO activity(ts, kind, mint, agent, title, subtitle, value)
            VALUES (?, 'foo', NULL, 'auditor', 'bad', '', '')
            """,
            (db.utc_now(),),
        )


def test_mode_and_exit_reason_checks(desk_db) -> None:
    _path, conn = desk_db
    ts = db.utc_now()
    conn.execute("INSERT INTO coins(mint) VALUES ('mint-check')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO signals(mint, ts, price, size_usd, stack_pct, link, mode, status)
            VALUES ('mint-check', ?, 1, 1, 1, 'x', 'DEMO', 'pending')
            """,
            (ts,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO positions(
                mint, opened_at, entry_price, size_usd, peak_price, stop_price,
                trail_active, closed_at, exit_price, exit_reason, pnl_usd, fees_usd, mode
            )
            VALUES ('mint-check', ?, 1, 1, 1, 1, 0, ?, 1, 'panic', 0, 0, 'PAPER')
            """,
            (ts, ts),
        )
    conn.execute(
        """
        INSERT INTO positions(
            mint, opened_at, entry_price, size_usd, peak_price, stop_price,
            trail_active, closed_at, exit_price, exit_reason, pnl_usd, fees_usd, mode
        )
        VALUES ('mint-check', ?, 1, 1, 1, 1, 0, NULL, NULL, NULL, NULL, NULL, 'LIVE')
        """,
        (ts,),
    )
    conn.commit()


def test_insert_price_rejects_unknown_writer(desk_db) -> None:
    _path, conn = desk_db
    conn.execute("INSERT INTO coins(mint) VALUES ('mint-writer')")
    with pytest.raises(ValueError):
        db.insert_price(conn, "other", "mint-writer", db.utc_now(), 1.0, 0.0)
