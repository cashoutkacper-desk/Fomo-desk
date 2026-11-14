"""STEP 13 item 4 · test_executor."""

from __future__ import annotations

import config
from agents.executor import Executor, size_ticket
from links import coin_url
from tests.fakes import MINT, add_coin, dex_view, make_ctx, run

CFG = config.defaults()


def test_stack_200_gives_16_then_bump_to_47_50():
    r = size_ticket(200, 200, 1_000_000, CFG)
    assert round(r["raw"], 2) == 16.00 and r["size"] == 47.50 and r["skip"] is None


def test_stack_200_skip_mode_skips():
    r = size_ticket(200, 200, 1_000_000, {**CFG, "executor.below_min": "skip"})
    assert r["skip"] == "under $47.50"


def test_paper_200_with_skip_warns_every_coin_skipped():
    chk = config.ticket_check({**CFG, "executor.below_min": "skip"}, 200)
    assert not chk["ok"] and chk["message"].startswith("every coin will be skipped, stack under $593.75")


def test_paper_200_with_bump_tells_tickets_bumped():
    chk = config.ticket_check(CFG, 200)
    assert chk["ok"] and chk["message"] == "tickets bumped to $47.50 = 23.8% of stack"
    assert round(config.min_stack(CFG), 2) == 593.75


def test_pool_40000_caps_size_at_800():
    r = size_ticket(1_000_000, 1_000_000, 40_000, CFG)
    assert r["size"] == 800.00 and r["cap"] == "max_pool_pct"


def test_no_free_stack():
    r = size_ticket(200, 30, 1_000_000, CFG)
    assert r["skip"] == "no free stack"


def _msg(score=82, price_at_analyst=0.001):
    return {"scanner": {"mint": MINT, "liquidityUsd": 50000},
            "auditor": {"passed": True},
            "analyst": {"agent": "analyst", "mint": MINT, "score": score, "passed": True},
            "priceAtAnalyst": price_at_analyst}


def test_paper_push_fill_and_activity(tmp_path):
    ctx = make_ctx(tmp_path)
    add_coin(ctx)
    ctx.src.dex.views[MINT] = dex_view(price=0.001, liq=50000)
    out = run(Executor(ctx).handle(_msg()))
    assert out["status"] == "bought" and out["sizeUsd"] == 47.50 and out["link"] == coin_url(MINT, "solana")
    push = ctx.notifier.sent[-1]
    assert push["text"] == ("PAPER · Buy Frog · $47.50 · 23.75% of stack\n"
                            "Auditor 8/8 · Analyst 82 · stop -6% · trail 12%")
    assert push["buttons"][0] == {"text": "Buy on FOMO", "url": coin_url(MINT, "solana")}
    assert [b["text"] for b in push["buttons"]] == ["Buy on FOMO", "Bought", "Skip"]
    pos = ctx.conn.execute("SELECT entry_price, size_usd, stop_price, mode FROM positions").fetchone()
    assert tuple(pos) == (0.001, 47.5, 0.001 * 0.94, "PAPER")
    sig = ctx.conn.execute("SELECT price, status, link FROM signals").fetchone()
    assert tuple(sig) == (0.001, "bought", coin_url(MINT, "solana"))
    a = ctx.conn.execute("SELECT title, subtitle, value FROM activity").fetchone()
    assert a[0] == "Bought Frog" and a[1].startswith("Executor · ") and a[2] == "$47.50 · 23.75% of stack"


def test_skip_mode_end_to_end(tmp_path):
    ctx = make_ctx(tmp_path, below_min="skip")
    add_coin(ctx)
    ctx.src.dex.views[MINT] = dex_view()
    out = run(Executor(ctx).handle(_msg()))
    assert out["status"] == "skipped" and out["reason"] == "under $47.50" and ctx.notifier.sent == []


def test_second_pass_same_night_ignored(tmp_path):
    ctx = make_ctx(tmp_path)
    add_coin(ctx)
    ctx.src.dex.views[MINT] = dex_view()
    ex = Executor(ctx)
    run(ex.handle(_msg()))
    assert run(ex.handle(_msg())) is None
    assert ctx.conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1


def test_rechecks(tmp_path):
    ctx = make_ctx(tmp_path)
    add_coin(ctx)
    ctx.src.dex.views[MINT] = dex_view(price=0.00116)
    assert run(Executor(ctx).handle(_msg()))["reason"] == "ran before the push"
    ctx2 = make_ctx(tmp_path / "b") if (tmp_path / "b").mkdir() is None else None
    add_coin(ctx2)
    ctx2.src.dex.views[MINT] = dex_view(liq=39000)
    assert run(Executor(ctx2).handle(_msg()))["reason"] == "liquidity dropping"


def test_no_price_skips(tmp_path):
    ctx = make_ctx(tmp_path)
    add_coin(ctx)
    out = run(Executor(ctx).handle(_msg()))
    assert out["reason"] == "no price"


def test_push_failed_paper_fills_anyway(tmp_path):
    ctx = make_ctx(tmp_path)
    add_coin(ctx)
    ctx.src.dex.views[MINT] = dex_view()
    ctx.notifier.fail_times = 4   # first try + 3 retries all fail
    out = run(Executor(ctx).handle(_msg()))
    assert out["status"] == "bought" and out["reason"] == "push failed"
