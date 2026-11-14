"""STEP 10 · settings: every key + default, ranges refuse with the range, ticket check
(on change and at the start of every night), push_rejects."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

import config
import main
import telegram
from agents.auditor import Auditor
from links import coin_url
from tests.fakes import MINT, POOL, add_coin, make_ctx, run

STEP10 = {
    "scanner.min_liquidity_usd": 40000, "scanner.max_top10_pct": 25, "scanner.max_h1_change_pct": 80,
    "scanner.poll_s": 60, "scanner.min_pool_age_min": 10, "scanner.max_wait_min": 60, "scanner.runner_h24_pct": 300,
    "auditor.enabled": True, "auditor.max_dev_pct": 5, "auditor.max_top10_pct": 25, "auditor.min_lp_locked_pct": 99,
    "auditor.min_pool_age_min": 10, "auditor.max_bundled": 5, "auditor.max_delegated_accounts": 10, "auditor.retries": 3,
    "analyst_mode": "jev", "analyst.min_alive_6h": 0.70, "analyst.min_organic": 0.65, "analyst.max_chase": 0.30,
    "analyst.min_score": 70, "analyst.timeout_s": 10,
    "analyst.rules.alive_volume_h1": 25000, "analyst.rules.organic_buys_h1": 150, "analyst.rules.bundled_penalty": 0.5,
    "analyst.rules.bundled_min_wallets": 3, "analyst.rules.flush_drop_m5_pct": 5, "analyst.rules.chase_h1_pct": 80,
    "executor.stack_pct": 8, "executor.max_pool_pct": 2, "executor.min_ticket_usd": 47.50, "executor.below_min": "bump",
    "executor.recheck_liq_drop_pct": 20, "executor.recheck_price_up_pct": 15, "executor.confirm_window_min": 5,
    "risk.poll_s": 15, "risk.stop_pct": 6, "risk.trail_start_pct": 20, "risk.trail_pct": 12, "risk.timeout_h": 3,
    "risk.timeout_min_gain_pct": 5, "risk.confirm_window_min": 2,
    "mode": "PAPER", "paper_start_stack": 200, "report_time": "08:00", "notifier": "telegram",
    "rpc_url": "https://api.mainnet-beta.solana.com", "push_rejects": False, "fees_ref_discount": 0.10, "fomo_handle": "",
}


def test_every_step10_key_with_its_spec_default():
    d = config.defaults()
    for k, v in STEP10.items():
        assert k in config.SPEC_BY_KEY, k
        assert config.SPEC_BY_KEY[k].default == v, k
        assert config.SPEC_BY_KEY[k].step10, k
    assert config.SPEC_BY_KEY["notifier"].choices == ("telegram", "webpush", "channel")
    assert config.SPEC_BY_KEY["executor.below_min"].choices == ("bump", "skip")
    assert config.SPEC_BY_KEY["analyst_mode"].choices == ("jev", "rules")
    assert len(d) == len(config.SPECS)


@pytest.mark.parametrize("spec", [s for s in config.SPECS if s.kind in ("int", "float")], ids=lambda s: s.key)
def test_every_number_has_min_max_and_refuses_out_of_range(spec):
    assert spec.lo is not None and spec.hi is not None and spec.lo < spec.hi
    assert spec.lo <= spec.default <= spec.hi
    span = spec.hi - spec.lo
    for bad in (spec.hi + max(1, span), ):
        with pytest.raises(config.SettingError) as e:
            config.coerce(spec.key, bad)
        assert f"between {config._fmt(spec.lo)} and {config._fmt(spec.hi)}" in str(e.value)
    if spec.key != "risk.stop_pct":          # "-8%" is read as an 8% stop
        with pytest.raises(config.SettingError):
            config.coerce(spec.key, spec.lo - max(1, span))


def test_other_kinds_validate():
    with pytest.raises(config.SettingError, match="HH:MM"):
        config.coerce("report_time", "25:00")
    with pytest.raises(config.SettingError, match="https://"):
        config.coerce("rpc_url", "http://x")
    with pytest.raises(config.SettingError, match="one of telegram, webpush, channel"):
        config.coerce("notifier", "sms")
    with pytest.raises(config.SettingError, match="true or false"):
        config.coerce("push_rejects", "maybe")


def test_min_stack_and_both_messages():
    cfg = config.defaults()
    assert config.min_stack(cfg) == 593.75
    assert config.ticket_check(cfg, 600)["message"] is None
    assert config.ticket_check(cfg, 200)["message"] == "tickets bumped to $47.50 = 23.8% of stack"
    skip = config.ticket_check({**cfg, "executor.below_min": "skip"}, 200)
    assert not skip["ok"]
    assert skip["message"].startswith("every coin will be skipped, stack under $593.75")
    assert "bump" in skip["message"] and "raise the stack" in skip["message"]


@pytest.mark.parametrize("key,val", [("executor.stack_pct", 8), ("executor.below_min", "skip"), ("live_stack", 1240),
                                     ("paper_start_stack", 300)])
def test_ticket_check_runs_on_stack_pct_below_min_stack_change(tmp_path, key, val):
    ctx = make_ctx(tmp_path)
    assert config.set_value(ctx.conn, key, val)["ticket"] is not None


def test_telegram_set_replies_with_ticket_message(tmp_path):
    ctx = make_ctx(tmp_path)
    desk = telegram.Desk(ctx.conn, clock=ctx.clock, log_dir=tmp_path)
    out = run(desk.handle("set below_min skip"))[0]["text"]
    assert out.startswith("below_min bump -> skip\nevery coin will be skipped, stack under $593.75")
    out = run(desk.handle("set below_min bump"))[0]["text"]
    assert out == "below_min skip -> bump\ntickets bumped to $47.50 = 23.8% of stack"


def test_ticket_check_at_the_start_of_every_night(tmp_path):
    ctx = make_ctx(tmp_path)
    first = ctx.night_start()

    async def go():
        t = asyncio.create_task(main.night_loop(ctx, first, every_s=0.01))
        await asyncio.sleep(0.05)
        assert ctx.notifier.sent == []                       # same night: no repeat
        ctx.clock.advance(hours=24)                          # next report_time passes
        await asyncio.sleep(0.05)
        t.cancel()
    run(go())
    texts = [m["text"] for m in ctx.notifier.sent]
    assert texts == ["tickets bumped to $47.50 = 23.8% of stack"]


def _freeze_scan(ctx):
    add_coin(ctx)
    ctx.src.rpc.freeze_authority = "FreezeAuth1111111111111111111111111111111111"
    return {"agent": "scanner", "mint": MINT, "name": "Frog", "pool": POOL, "liquidityUsd": 50000.0}


def test_push_rejects_false_by_default_no_push(tmp_path):
    ctx = make_ctx(tmp_path)
    run(Auditor(ctx).audit(_freeze_scan(ctx)))
    assert ctx.notifier.sent == []


def test_push_rejects_true_pushes_the_reject_with_fomo_link(tmp_path):
    ctx = make_ctx(tmp_path, push_rejects=True)
    run(Auditor(ctx).audit(_freeze_scan(ctx)))
    (m,) = ctx.notifier.sent
    assert m["text"] == "PAPER · Rejected Frog · Auditor · freeze not revoked"
    assert m["buttons"] == [{"text": "Open on FOMO", "url": coin_url(MINT, "solana")}]


def test_restart_in_the_same_night_does_not_repeat_the_check(tmp_path):
    ctx = make_ctx(tmp_path)
    run(main.check_night_once(ctx))
    run(main.check_night_once(ctx))                         # restart, same night
    assert [m["text"] for m in ctx.notifier.sent] == ["tickets bumped to $47.50 = 23.8% of stack"]
    ctx.clock.advance(hours=24)
    run(main.check_night_once(ctx))
    assert len(ctx.notifier.sent) == 2
