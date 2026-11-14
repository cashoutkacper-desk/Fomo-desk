"""STEP 11 · PAPER, THEN LIVE: fees table, a deterministic PAPER round trip, LIVE
[Bought]/[Sold], the LIVE switch gate, and hard wall-clock caps on network awaits."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta

import pytest

import bus
import config
import db
import fees
import shapes
import sources
from agents.executor import Executor
from agents.risk import Risk
from links import coin_url
from notifier import TelegramError, TelegramNotifier
from runtime import iso
from tests.fakes import MINT, add_coin, dex_view, make_ctx, run

# ---------------------------------------------------------------- fees

FEES = [  # ticket, per side, round trip (same ticket out), with this desk's discount 0
    (5.00, 0.10, 0.20), (20.00, 0.40, 0.80), (47.50, 0.95, 1.90),
    (100.00, 0.95, 1.90), (190.00, 0.95, 1.90), (500.00, 2.50, 5.00),
]


@pytest.mark.parametrize("ticket,side,rt", FEES)
def test_fees_table(ticket, side, rt):
    assert round(fees.fee_usd(ticket, 0.0), 2) == side
    assert round(fees.round_trip_fees(ticket, ticket, 0.0), 2) == rt
    assert round(fees.fee_usd(ticket, 0.10), 4) == round(side * 0.9, 4)    # through the ref


def test_desk_discount_is_zero_and_gas_is_zero():
    assert config.desk_default("fees_ref_discount") == 0
    assert fees.base_fee_usd(47.49) == pytest.approx(0.9498) and fees.base_fee_usd(190.01) == pytest.approx(0.95005)


# ---------------------------------------------------------------- PAPER round trip

def _analyst_msg():
    return {"scanner": {"mint": MINT, "liquidityUsd": 50000}, "auditor": {"passed": True},
            "analyst": {"agent": "analyst", "mint": MINT, "score": 82, "passed": True}, "priceAtAnalyst": 0.001}


def test_paper_replay_fill_at_signal_exit_at_firing_price_fees_balance_labels(tmp_path):
    ctx = make_ctx(tmp_path, fees_ref_discount=0)
    add_coin(ctx)
    start = config.load(ctx.conn)["paper_start_stack"]
    ctx.conn.execute("INSERT INTO balance(ts, value) VALUES (?,?)", (iso(ctx.now()), start))
    ctx.conn.commit()
    assert config.current_stack(ctx.conn, config.load(ctx.conn)) == 200.0       # starts at paper_start_stack

    # push goes out at DexScreener 0.001 -> fill at that signal price, at once
    ctx.src.dex.views[MINT] = dex_view(price=0.001, liq=50000)
    out = run(Executor(ctx).handle(_analyst_msg()))
    assert out["status"] == "bought" and out["signalPrice"] == 0.001 and out["mode"] == "PAPER"
    pos = ctx.conn.execute("SELECT * FROM positions").fetchone()
    assert pos["entry_price"] == 0.001 and pos["size_usd"] == 47.5 and pos["mode"] == "PAPER"
    assert ctx.notifier.sent[-1]["text"].startswith("PAPER · Buy Frog · $47.50")

    # price gaps through the -6% stop: 0.00093 fires it -> exit at 0.00093, not at the 0.00094 line
    ctx.clock.advance(seconds=15)
    ctx.src.dex.views[MINT] = dex_view(price=0.00093, liq=50000)
    risk = Risk(ctx)
    exits = run(risk.poll_once())
    (e,) = exits
    assert e["reason"] == "stop" and e["exitPrice"] == 0.00093 and e["status"] == "closed"
    exit_usd = 47.5 * 0.93
    fee = fees.round_trip_fees(47.5, exit_usd, 0.0)                            # 0.95 + 2% of 44.175
    assert e["feesUsd"] == round(fee, 2) == 1.83 and e["pnlUsd"] == round(exit_usd - 47.5 - fee, 2) == -5.16
    bal = ctx.conn.execute("SELECT value FROM balance ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    assert bal == round(200 + e["pnlUsd"], 2)                                  # balance simulated
    push = ctx.notifier.sent[-1]
    assert push["text"] == f"PAPER · Frog hit the -6% stop · -${abs(e['pnlUsd']):,.2f}"
    assert push["buttons"] == [{"text": "Sell on FOMO", "url": coin_url(MINT, "solana")}]   # no [Sold] in PAPER
    # every screen / JSON says PAPER
    st = shapes.state(ctx.conn)
    assert st["mode"] == "PAPER" and st["balance"] == bal
    assert shapes.account(ctx.conn)["mode"] == "PAPER"
    assert all(p["mode"] == "PAPER" for p in ctx.conn.execute("SELECT mode FROM positions"))


def test_ticket_check_runs_before_the_first_coin(tmp_path):
    ctx = make_ctx(tmp_path)
    add_coin(ctx)
    calls = []

    async def hook():
        calls.append(len(ctx.notifier.sent))
    ctx.night_hook = hook
    ctx.src.dex.views[MINT] = dex_view()
    run(Executor(ctx).handle(_analyst_msg()))
    assert calls == [0]                     # hook ran before the buy push


# ---------------------------------------------------------------- LIVE pipeline

def _live(ctx):
    db.set_setting(ctx.conn, "mode", "LIVE", "test")
    db.set_setting(ctx.conn, "live_stack", "1000", "test")
    ctx.conn.commit()


def test_live_bought_opens_the_position(tmp_path):
    ctx = make_ctx(tmp_path)
    add_coin(ctx)
    _live(ctx)
    ctx.src.dex.views[MINT] = dex_view(price=0.001)
    ex = Executor(ctx)

    async def go():
        cb = asyncio.create_task(ex.callbacks_loop())
        h = asyncio.create_task(ex.handle(_analyst_msg()))
        await asyncio.sleep(0.05)
        assert ctx.conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0   # nothing until the tap
        push = ctx.notifier.sent[-1]
        assert not push["text"].startswith("PAPER") and [b["text"] for b in push["buttons"]] == ["Buy on FOMO", "Bought", "Skip"]
        await bus.executor_callbacks.put(("bought", MINT))
        out = await asyncio.wait_for(h, 2)
        cb.cancel()
        return out
    out = run(go())
    assert out["status"] == "bought" and out["mode"] == "LIVE"
    pos = ctx.conn.execute("SELECT entry_price, mode FROM positions").fetchone()
    assert tuple(pos) == (0.001, "LIVE")


def test_live_sold_closes_the_position(tmp_path):
    ctx = make_ctx(tmp_path)
    add_coin(ctx)
    _live(ctx)
    ctx.conn.execute("INSERT INTO positions(mint, opened_at, entry_price, size_usd, peak_price, stop_price,"
                     " trail_active, mode) VALUES (?,?,?,?,?,?,?,?)",
                     (MINT, iso(ctx.now()), 0.001, 80.0, 0.001, 0.00094, 0, "LIVE"))
    ctx.conn.commit()
    ctx.src.dex.views[MINT] = dex_view(price=0.0009)
    risk = Risk(ctx)

    async def go():
        (e,) = await risk.poll_once()
        assert e["status"] == "exiting"
        assert ctx.conn.execute("SELECT closed_at FROM positions").fetchone()[0] is None   # waits for [Sold]
        assert [b["text"] for b in ctx.notifier.sent[-1]["buttons"]] == ["Sell on FOMO", "Sold"]
        loop = asyncio.create_task(risk.live_loop())
        await bus.risk_callbacks.put(("sold", MINT))
        for _ in range(100):
            await asyncio.sleep(0.01)
            if ctx.conn.execute("SELECT closed_at FROM positions").fetchone()[0]:
                break
        loop.cancel()
    run(go())
    row = ctx.conn.execute("SELECT closed_at, exit_reason, exit_price FROM positions").fetchone()
    assert row[0] is not None and row[1] == "stop" and row[2] == 0.0009
    # LIVE never touches the simulated balance (no money moves by us)
    assert ctx.conn.execute("SELECT COUNT(*) FROM balance").fetchone()[0] == 0


# ---------------------------------------------------------------- LIVE switch gate

def _full_paper_night(ctx):
    cfg = config.load(ctx.conn)
    ns = config.night_start(cfg, ctx.now() - timedelta(days=1))
    from runtime import parse_iso
    t = parse_iso(ns)
    for i in range(0, 24 * 60 + 1, 5):
        ctx.conn.execute("INSERT INTO balance(ts, value) VALUES (?,?)", (iso(t + timedelta(minutes=i)), 200))
    ctx.conn.commit()


def test_live_refused_without_typing_LIVE_and_without_a_full_paper_night(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    with pytest.raises(config.SettingError, match="confirm"):
        config.set_mode(ctx.conn, "LIVE")                             # no typed LIVE
    with pytest.raises(config.SettingError, match="full PAPER night"):
        config.set_mode(ctx.conn, "LIVE", confirm="LIVE")             # typed, but no PAPER night
    assert config.load(ctx.conn)["mode"] == "PAPER"
    monkeypatch.setattr(config, "full_paper_night", lambda conn, now=None, max_gap_min=10: True)
    with pytest.raises(config.SettingError):
        config.set_mode(ctx.conn, "LIVE", confirm="live")             # must be exactly LIVE
    assert config.set_mode(ctx.conn, "LIVE", confirm="LIVE")["new"] == "LIVE"


def test_full_paper_night_detects_a_real_night(tmp_path):
    ctx = make_ctx(tmp_path)
    assert config.full_paper_night(ctx.conn, now=ctx.now()) is False
    _full_paper_night(ctx)
    assert config.full_paper_night(ctx.conn, now=ctx.now()) is True


# ---------------------------------------------------------------- wall-clock caps

class HangingClient:
    async def post(self, *a, **k):
        await asyncio.sleep(3600)

    async def get(self, *a, **k):
        await asyncio.sleep(3600)


def test_notifier_call_has_a_hard_wall_clock_cap():
    n = TelegramNotifier(owner_chat_id=1, client=HangingClient(), token_getter=lambda: "1:x")
    n.wall_margin_s = 0.05
    t0 = time.monotonic()
    with pytest.raises(TelegramError) as e:
        run(n._call("getUpdates", {"timeout": 0}, timeout=0.05))
    assert time.monotonic() - t0 < 2
    assert "TimeoutError" in str(e.value) and "1:x" not in str(e.value)


def test_push_survives_a_hung_telegram():
    n = TelegramNotifier(owner_chat_id=1, client=HangingClient(), token_getter=lambda: "1:x")
    n.wall_margin_s = 0.01
    n.retry_delay_s = 0

    async def fast_call(method, payload, timeout=20):
        return await TelegramNotifier._call(n, method, payload, timeout=0.01)
    n._call = fast_call
    assert run(n.push("x", mode="PAPER", retries=1)) is False   # returns, never freezes


def test_sources_calls_are_capped(monkeypatch):
    orig = sources.capped
    monkeypatch.setattr(sources, "capped", lambda aw, s, what: orig(aw, 0.05, what))
    dex = sources.Dex(HangingClient(), max_rpm=100)
    rug = sources.RugCheck(HangingClient())
    rpc = sources.Rpc(HangingClient(), lambda: "https://rpc.example")
    for coro in (dex.tokens([MINT]), rug.report(MINT), rpc.call("getSlot", [])):
        t0 = time.monotonic()
        with pytest.raises(sources.SourceError, match="timeout"):
            run(coro)
        assert time.monotonic() - t0 < 2
