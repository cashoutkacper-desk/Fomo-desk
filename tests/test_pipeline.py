"""Nov 19 '0 activity' check: the wiring scanner -> bus -> Auditor.run -> db -> activity.json
works; Activity stays empty only while the Scanner kills every coin (Activity rows come
from Auditor/Analyst/Executor/Risk, Scanner kills live in the Scanner card feed)."""

from __future__ import annotations

import asyncio

import bus
import shapes
from agents.auditor import Auditor
from agents.scanner import Scanner
from tests.fakes import MINT, dex_view, make_ctx, run


async def _migrate_and_wait(ctx, s, liq):
    assert await s.on_migration({"signature": "sig", "mint": MINT, "txType": "migrate", "pool": "pump-amm"}) == MINT
    ctx.src.dex.views[MINT] = dex_view(liq=liq, changeH1=10)
    ctx.clock.advance(minutes=11)
    return await s.poll_once()


def test_scanner_pass_reaches_running_auditor_and_shows_in_activity(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.src.rpc.freeze_authority = "FreezeAuth1111111111111111111111111111111111"   # auditor rejects at layer 2

    async def go():
        aud = asyncio.create_task(Auditor(ctx).run())       # the real loop main.py starts
        d = await _migrate_and_wait(ctx, Scanner(ctx), 60000)
        for _ in range(200):
            await asyncio.sleep(0.01)
            if ctx.conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]:
                break
        aud.cancel()
        return d
    d = run(go())
    assert d[0]["verdict"] == "pass"
    assert ctx.conn.execute("SELECT COUNT(*) FROM audits WHERE mint=?", (MINT,)).fetchone()[0] == 8
    rows = shapes.activity(ctx.conn)["rows"]
    assert len(rows) == 1 and rows[0]["title"] == "Rejected Frog" and rows[0]["subtitle"] == "Auditor · freeze not revoked"


def test_liquidity_kill_never_reaches_auditor_and_activity_stays_empty(tmp_path):
    ctx = make_ctx(tmp_path)

    async def go():
        return await _migrate_and_wait(ctx, Scanner(ctx), 37000)    # like CATMARIO at minute 10 today
    d = run(go())
    assert d[0]["verdict"] == "kill" and d[0]["reason"] == "liq $37.0K"
    assert bus.scanner_to_auditor.empty()
    assert shapes.activity(ctx.conn)["rows"] == []
    feed = shapes.decisions(ctx.conn, "scanner")                       # the kill is in the Scanner feed
    assert "liq $37.0K" in str(feed)
