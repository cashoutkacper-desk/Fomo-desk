"""STEP 13 item 3 · test_risk."""

from __future__ import annotations

from datetime import timedelta

import config
from agents.risk import Risk, exit_line, pnl, step, stop_pct_of
from links import coin_url
from runtime import iso
from tests.fakes import MINT, add_coin, dex_view, make_ctx, run

CFG = config.defaults()


def _pos(entry=1.0, stop_pct=6.0, opened="2026-11-14T03:00:00.000Z"):
    return {"id": 1, "entry_price": entry, "size_usd": 47.5, "peak_price": entry,
            "stop_price": entry * (1 - stop_pct / 100), "trail_active": 0, "opened_at": opened}


def _now(h=0.0):
    from runtime import parse_iso
    return parse_iso("2026-11-14T03:00:00.000Z") + timedelta(hours=h)


def test_trail_exit_after_25_then_13_off_top():
    p = _pos()
    assert step(p, 1.25, _now(0.1), CFG)["exit"] is None and p["trail_active"] == 1
    assert step(p, 1.25 * 0.90, _now(0.2), CFG)["exit"] is None      # 10% off the top: hold
    p2 = _pos()
    step(p2, 1.25, _now(0.1), CFG)
    assert step(p2, 1.25 * 0.87, _now(0.2), CFG)["exit"] == "trail"


def test_stop_at_minus_6_1():
    assert step(_pos(), 0.939, _now(0.1), CFG)["exit"] == "stop"
    assert step(_pos(), 0.941, _now(0.1), CFG)["exit"] is None


def test_timeout_3h_at_plus_4():
    assert step(_pos(), 1.04, _now(3.0), CFG)["exit"] == "timeout"
    assert step(_pos(), 1.04, _now(2.9), CFG)["exit"] is None
    assert step(_pos(), 1.06, _now(3.0), CFG)["exit"] is None


def test_trail_never_turns_off_and_exit_line():
    p = _pos()
    step(p, 1.3, _now(0.1), CFG)
    step(p, 1.18, _now(0.2), CFG)  # back under +20% but above the trail line (1.144)
    assert p["trail_active"] == 1
    assert abs(exit_line(p, CFG) - 1.3 * 0.88) < 1e-12


def test_loosening_never_moves_open_stop_down():
    p = _pos()
    cfg = {**CFG, "risk.stop_pct": 10.0}
    step(p, 1.0, _now(0.1), cfg)
    assert abs(p["stop_price"] - 0.94) < 1e-12 and stop_pct_of(p) == 6
    cfg = {**CFG, "risk.stop_pct": 4.0}            # tighter applies to the open position
    step(p, 1.0, _now(0.2), cfg)
    assert abs(p["stop_price"] - 0.96) < 1e-12 and stop_pct_of(p) == 4


def test_paper_pnl_minus_fees_both_sides():
    p = _pos()
    usd, pct, f = pnl(p, 1.1, 0.10)
    # entry $47.50 -> 0.95*0.9 ; exit $52.25 -> 0.95*0.9
    assert f == round(0.855 * 2, 2) and usd == round(4.75 - 1.71, 2)


def _open(ctx, risk_mod_executor=None, entry=0.001, size=47.5, mint=MINT, minutes_ago=0):
    """Open a PAPER position the way Executor.fill does (stop from the live setting)."""
    from agents.executor import Executor
    ex = Executor(ctx)
    out = {"mint": mint, "signalPrice": entry, "sizeUsd": size, "stackPct": 23.75, "mode": "PAPER",
           "link": coin_url(mint, "solana"), "status": "sent"}
    sig = ctx.conn.execute("INSERT INTO signals(mint, ts, price, size_usd, stack_pct, link, mode, status)"
                           " VALUES (?,?,?,?,?,?,?,?)", (mint, ctx.now_iso(), entry, size, 23.75,
                                                         out["link"], "PAPER", "sent")).lastrowid

    async def go():
        await ex.fill(out, sig, "Frog" if mint == MINT else "Toad", ctx.now_iso())
    run(go())
    return ctx.conn.execute("SELECT * FROM positions WHERE mint=? ORDER BY id DESC", (mint,)).fetchone()


def test_set_stop_to_minus_8_scenario(tmp_path):
    ctx = make_ctx(tmp_path)
    old_mint = "OldMint1111111111111111111111111111111pump"
    add_coin(ctx)
    add_coin(ctx, mint=old_mint, name="Toad")
    old = _open(ctx, mint=old_mint)                       # opened at -6%
    res = config.set_value(ctx.conn, "stop", "-8%", who="owner")   # "set stop to -8%"
    assert res["message"] == "stop -6% -> -8%" and res["key"] == "risk.stop_pct"
    assert config.load(ctx.conn)["risk.stop_pct"] == 8.0
    new = _open(ctx)                                       # new PAPER position
    assert stop_pct_of(dict(new)) == 8 and stop_pct_of(dict(old)) == 6
    # Risk pill reads the live setting
    assert f"stop -{config.load(ctx.conn)['risk.stop_pct']:g}%" == "stop -8%"
    ctx.src.dex.views[MINT] = dex_view(price=0.001 * (1 - 0.081))       # new one drops 8.1%
    ctx.src.dex.views[old_mint] = dex_view(price=0.001 * (1 - 0.05), name="Toad")  # old one -5%
    r = Risk(ctx)
    exits = run(r.poll_once())
    assert len(exits) == 1 and exits[0]["reason"] == "stop" and exits[0]["stopPct"] == 8
    pushes = [m for m in ctx.notifier.sent if m["kind"] == "exit"]
    assert pushes[0]["text"].startswith("PAPER · Frog hit the -8% stop · -$")
    assert pushes[0]["buttons"] == [{"text": "Sell on FOMO", "url": coin_url(MINT, "solana")}]
    act = ctx.conn.execute("SELECT title, subtitle, value FROM activity WHERE kind='stopped'").fetchone()
    assert act[0] == "Stopped Frog" and act[1] == "Risk · -8% stop" and act[2].startswith("-$")
    # the -6% position is still open and still shows -6% (looser setting never moved it down)
    o = ctx.conn.execute("SELECT * FROM positions WHERE mint=?", (old_mint,)).fetchone()
    assert o["closed_at"] is None and stop_pct_of(dict(o)) == 6
    ctx.src.dex.views[old_mint] = dex_view(price=0.001 * (1 - 0.061), name="Toad")
    exits = run(r.poll_once())
    assert exits[0]["stopPct"] == 6
    assert ctx.notifier.sent[-1]["text"].startswith("PAPER · Toad hit the -6% stop")


def test_trail_exit_end_to_end_push_and_activity(tmp_path):
    ctx = make_ctx(tmp_path)
    add_coin(ctx)
    _open(ctx)
    r = Risk(ctx)
    ctx.src.dex.views[MINT] = dex_view(price=0.00125)
    assert run(r.poll_once()) == []
    ctx.src.dex.views[MINT] = dex_view(price=0.00125 * 0.87)
    ex = run(r.poll_once())[0]
    assert ex["reason"] == "trail" and ex["status"] == "closed" and ex["link"] == coin_url(MINT, "solana")
    assert ctx.notifier.sent[-1]["text"].startswith("PAPER · Sold Frog · +$")
    a = ctx.conn.execute("SELECT title, subtitle, value FROM activity WHERE kind='sold'").fetchone()
    assert tuple(a)[:2] == ("Sold Frog", "Risk · trailing stop") and a[2].startswith("+$")
    bal = ctx.conn.execute("SELECT value FROM balance ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    assert abs(bal - (200 + ex["pnlUsd"])) < 0.01


def test_timeout_push_text(tmp_path):
    ctx = make_ctx(tmp_path)
    add_coin(ctx)
    _open(ctx)
    ctx.clock.advance(hours=3)
    ctx.src.dex.views[MINT] = dex_view(price=0.00104)
    ex = run(Risk(ctx).poll_once())[0]
    assert ex["reason"] == "timeout"
    assert ctx.notifier.sent[-1]["text"].startswith("PAPER · Sell Frog, dead money · ")
    assert ctx.notifier.sent[-1]["text"].endswith("% in 3h")


def test_no_price_4_polls_push_never_invents(tmp_path):
    ctx = make_ctx(tmp_path)
    add_coin(ctx)
    _open(ctx)
    r = Risk(ctx)
    ctx.src.dex.views.pop(MINT, None)
    for _ in range(4):
        assert run(r.poll_once()) == []
    assert ctx.notifier.sent[-1]["text"] == "PAPER · Frog: no price for 1 min"
    assert ctx.notifier.sent[-1]["buttons"][0]["url"] == coin_url(MINT, "solana")
    assert ctx.conn.execute("SELECT closed_at FROM positions").fetchone()[0] is None


def test_liquidity_zero_exits_pulled(tmp_path):
    ctx = make_ctx(tmp_path)
    add_coin(ctx)
    _open(ctx)
    ctx.src.dex.views[MINT] = dex_view(price=0.0009, liq=0)
    ex = run(Risk(ctx).poll_once())[0]
    assert ex["reason"] == "pulled"
