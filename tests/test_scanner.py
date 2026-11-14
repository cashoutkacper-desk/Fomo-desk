"""Scanner: kill order, copycat, intake filter, never passes unknown/young coins."""

from __future__ import annotations

import bus
import config
from agents.scanner import Scanner, copycat_of, kill_reason, norm_name
from tests.fakes import MINT, POOL, dex_view, make_ctx, run

CFG = config.defaults()


def test_kill_order_and_texts():
    assert kill_reason(31200, 38, 112, "X", CFG) == "liq $31.2K"
    assert kill_reason(50000, 38, 112, "X", CFG) == "top 10 hold 38%"
    assert kill_reason(50000, 20, 112, "X", CFG) == "+112% in 1h"
    assert kill_reason(50000, 20, 10, "Pepe", CFG) == "copycat of Pepe"
    assert kill_reason(50000, 20, 10, None, CFG) is None


def test_copycat():
    runners = {"R1": {"n": norm_name("Pepe Coin 🐸"), "s": norm_name("PEPE"), "label": "Pepe Coin"}}
    assert norm_name("Pepe Coin 🐸 2") == "pepecoin"
    assert copycat_of("PEPE COIN 2.0", "PC2", "M", runners) == "Pepe Coin"
    assert copycat_of("Baby Pepe", "BPEPE", "M", runners) == "Pepe Coin"   # PEPE inside BPEPE
    assert copycat_of("Frog", "FRG", "M", runners) is None
    assert copycat_of("Pepe Coin", "PEPE", "R1", runners) is None           # not a copy of itself


def test_intake_filters_and_waits(tmp_path):
    ctx = make_ctx(tmp_path)
    s = Scanner(ctx)

    async def go():
        assert await s.on_migration({"message": "Successfully subscribed"}) is None
        assert await s.on_migration({"mint": "Other", "txType": "migrate", "pool": "raydium"}) is None
        assert await s.on_migration({"signature": "sig", "mint": MINT, "txType": "migrate",
                                     "pool": "pump-amm"}) == MINT
        # DexScreener doesn't know it yet: waits
        assert await s.poll_once() == []
        ctx.src.dex.views[MINT] = dex_view(liq=60000, changeH1=10)
        ctx.clock.advance(minutes=5)
        assert await s.poll_once() == []            # pool younger than 10 min: waits
        ctx.clock.advance(minutes=6)
        d = await s.poll_once()
        return d, bus.scanner_to_auditor.get_nowait()
    d, nxt = run(go())
    assert d[0]["verdict"] == "pass" and d[0]["pool"] == POOL and d[0]["ageMin"] == 11
    assert d[0]["top10Pct"] == 8.7 and nxt["mint"] == MINT
    coin = ctx.conn.execute("SELECT creator, creator_balance_at_migration FROM coins").fetchone()
    assert coin[0] is not None and coin[1] == 5_000_000_000      # creator from RugCheck, balance NOW
    assert ctx.conn.execute("SELECT COUNT(*) FROM prices WHERE writer='scanner'").fetchone()[0] == 2


def test_never_qualified_after_60_min(tmp_path):
    ctx = make_ctx(tmp_path)
    s = Scanner(ctx)

    async def go():
        await s.on_migration({"mint": MINT, "txType": "migrate", "pool": "pump-amm"})
        ctx.clock.advance(minutes=61)
        return await s.poll_once()
    d = run(go())
    assert d[0]["verdict"] == "kill" and d[0]["reason"] == "never qualified"


def test_same_mint_never_twice(tmp_path):
    ctx = make_ctx(tmp_path)
    s = Scanner(ctx)

    async def go():
        await s.on_migration({"mint": MINT, "txType": "migrate", "pool": "pump-amm"})
        ctx.src.dex.views[MINT] = dex_view(liq=10000)
        ctx.clock.advance(minutes=11)
        await s.poll_once()
        return await s.on_migration({"mint": MINT, "txType": "migrate", "pool": "pump-amm"})
    assert run(go()) is None


# ---------------- websocket reconnect alert (STEP 12 threshold, real disconnects only)

def test_15_quiet_reconnects_no_alert(tmp_path):
    ctx = make_ctx(tmp_path)
    s = Scanner(ctx)

    async def go():
        return [await s.on_reconnect("quiet", "idle", 0) for _ in range(15)]
    assert not any(run(go()))
    assert s.tracker.quiet == 15 and s.tracker.real == 0
    assert [m for m in ctx.notifier.sent if m["kind"] == "alert"] == []


def test_11_real_disconnects_in_an_hour_alert(tmp_path):
    ctx = make_ctx(tmp_path)
    s = Scanner(ctx)

    async def go():
        return [await s.on_reconnect("real", "ConnectionClosedError", 1) for _ in range(11)]
    res = run(go())
    assert res[:10] == [False] * 10 and res[10] is True
    alerts = [m for m in ctx.notifier.sent if m["kind"] == "alert"]
    assert len(alerts) == 1 and "11 times in the last hour" in alerts[0]["text"]


def test_real_disconnects_outside_the_hour_dont_count():
    from agents.scanner import ReconnectTracker
    t = ReconnectTracker()
    for i in range(10):
        assert t.record("real", i * 400.0) is False      # spread over > 1h
    for i in range(30):
        t.record("quiet", 5000.0 + i)
    assert t.real_last_hour(4000.0) < 11
