"""STEP 13 item 2 · test_gate: jev mode and rules mode."""

from __future__ import annotations

import pytest

import bus
import config
from agents.analyst import Analyst, build_state, gate, rules_answers
from tests.fakes import MINT, add_coin, dex_view, make_ctx, run

CFG = config.defaults()
AT = {"alive_6h": 0.70, "organic": 0.65, "flush": 0.75, "chase": 0.30}   # score exactly 70


def test_all_at_threshold_pass():
    score, passed, failed = gate(AT, CFG)
    assert score == 70 and passed and failed == []


@pytest.mark.parametrize("change, expect", [
    ({"alive_6h": 0.69}, "alive_6h 0.69"),
    ({"organic": 0.64}, "organic 0.64"),
    ({"chase": 0.31}, "chase 0.31"),
])
def test_each_threshold_rejects(change, expect):
    a = {**AT, "flush": 1.0, **change}       # flush 1.0 keeps the score above 70
    score, passed, failed = gate(a, CFG)
    assert not passed and failed[0] == expect and score >= 70


def test_score_69_rejects():
    a = {"alive_6h": 0.9, "organic": 0.9, "flush": 0.06, "chase": 0.1}
    score, passed, failed = gate(a, CFG)
    assert score == 69 and not passed and failed == ["score 69"]


def _msg(bundled_value="None"):
    layers = [{"n": n, "name": "x", "pass": True, "value": v, "source": "rpc"} for n, v in
              [(1, "Revoked"), (2, "Revoked"), (3, "0.9% · sold 0"), (4, "16.8%"), (5, "100% burned"),
               (6, "43m"), (7, bundled_value), (8, "None")]]
    return {"scanner": {"agent": "scanner", "mint": MINT, "name": "Frog", "liquidityUsd": 50000, "ageMin": 43},
            "auditor": {"agent": "auditor", "mint": MINT, "passed": True, "layers": layers}}


def _analyst(tmp_path, mode, answers=None, **dexkw):
    ctx = make_ctx(tmp_path, analyst_mode=mode)
    add_coin(ctx)
    ctx.src.dex.views[MINT] = dex_view(**dexkw)
    if answers:
        ctx.src.jev.answers = answers
    return ctx, Analyst(ctx)


def _activity(ctx):
    return [tuple(r) for r in ctx.conn.execute("SELECT title, subtitle, value FROM activity")]


@pytest.mark.parametrize("answers, sub", [
    ({**AT, "flush": 1.0, "alive_6h": 0.69}, "Analyst · alive_6h 0.69"),
    ({**AT, "flush": 1.0, "organic": 0.64}, "Analyst · organic 0.64"),
    ({**AT, "flush": 1.0, "chase": 0.31}, "Analyst · chase 0.31"),
    ({"alive_6h": 0.9, "organic": 0.9, "flush": 0.06, "chase": 0.1}, "Analyst · score 69"),
])
def test_jev_mode_rejects_write_activity(tmp_path, answers, sub):
    ctx, an = _analyst(tmp_path, "jev", answers)
    out = run(an.handle(_msg()))
    assert out["passed"] is False and out["model"] == "jev-1.14.0" and out["inputTokens"] == 300
    assert _activity(ctx) == [("Rejected Frog", sub, "Skipped")]
    row = ctx.conn.execute("SELECT pass, input_tokens FROM verdicts").fetchone()
    assert tuple(row) == (0, 300)


def test_jev_mode_at_threshold_passes_to_executor(tmp_path):
    ctx, an = _analyst(tmp_path, "jev", AT)

    async def go():
        out = await an.handle(_msg())
        return out, bus.analyst_to_executor.get_nowait()
    out, nxt = run(go())
    assert out["passed"] and out["score"] == 70 and nxt["analyst"]["mint"] == MINT
    q = ctx.src.jev.calls[0]["questions"]
    assert set(q) == {"alive_6h", "organic", "flush", "chase"} and all(v["type"] == "noul" for v in q.values())


def test_jev_request_shape_and_state_clean(tmp_path):
    ctx, an = _analyst(tmp_path, "jev", AT)
    run(an.handle(_msg()))
    state = ctx.src.jev.calls[0]["state"]
    assert state.startswith("Coin Frog on Solana, graduated 43 min ago.")
    import re
    assert not re.search(r"\bbuy\b", state, re.I)
    for t in ("0.7", "0.65", "0.3", "balance", "stack", "threshold"):  # no thresholds / balance
        assert t not in state
    assert len(state) <= 3000


def test_jev_http_error_retries_once_then_no_answer(tmp_path):
    ctx, an = _analyst(tmp_path, "jev", AT)
    ctx.src.jev.fail = 2
    out = run(an.handle(_msg()))
    assert not out["passed"] and out["failed"] == ["no answer"] and len(ctx.src.jev.calls) == 2
    assert _activity(ctx) == [("Rejected Frog", "Analyst · no answer", "Skipped")]


def test_jev_error_never_falls_back_to_rules(tmp_path):
    ctx, an = _analyst(tmp_path, "jev", AT)
    ctx.src.jev.fail = 5
    out = run(an.handle(_msg()))
    assert out["model"] != "rules" and out["failed"] == ["no answer"]


def test_jev_bad_answer(tmp_path):
    ctx, an = _analyst(tmp_path, "jev", {**AT, "chase": 1.4})
    out = run(an.handle(_msg()))
    assert out["failed"] == ["bad answer"]


def test_never_reasks_same_coin(tmp_path):
    ctx, an = _analyst(tmp_path, "jev", {**AT, "chase": 0.31, "flush": 1.0})
    run(an.handle(_msg()))
    assert run(an.handle(_msg())) is None and len(ctx.src.jev.calls) == 1


# ---------------- rules mode (fake DexScreener numbers)

def test_rules_formulas():
    cfg = config.defaults()
    d = dex_view(volumeH1=12500, buysH1=150, changeM5=-6, buysM5=50, sellsM5=40, changeH1=40)
    a = rules_answers(d, bundled=0, cfg=cfg)
    assert a == {"alive_6h": 0.5, "organic": 1.0, "flush": 1.0, "chase": 0.5}
    a = rules_answers(d, bundled=3, cfg=cfg)
    assert a["organic"] == 0.5                      # x0.5 from 3 bundled wallets
    assert rules_answers(dex_view(changeM5=-1), 0, cfg)["flush"] == 0.5
    assert rules_answers(dex_view(changeM5=2), 0, cfg)["flush"] == 0.0
    assert rules_answers(dex_view(changeH1=200, volumeH1=10**9), 0, cfg)["chase"] == 1.0


@pytest.mark.parametrize("dexkw, sub", [
    ({"volumeH1": 17250, "buysH1": 300, "changeH1": 0, "changeM5": -6, "buysM5": 9, "sellsM5": 1}, "alive_6h 0.69"),
    ({"volumeH1": 50000, "buysH1": 96, "changeH1": 0, "changeM5": -6, "buysM5": 9, "sellsM5": 1}, "organic 0.64"),
    ({"volumeH1": 50000, "buysH1": 300, "changeH1": 24.8, "changeM5": -6, "buysM5": 9, "sellsM5": 1}, "chase 0.31"),
    ({"volumeH1": 25000, "buysH1": 150, "changeH1": 20, "changeM5": 1, "buysM5": 9, "sellsM5": 1}, "score 69"),
])
def test_rules_mode_rejects(tmp_path, dexkw, sub):
    ctx, an = _analyst(tmp_path, "rules", **dexkw)
    out = run(an.handle(_msg()))
    assert out["model"] == "rules" and out["inputTokens"] == 0 and not out["passed"]
    assert _activity(ctx) == [("Rejected Frog", f"Analyst (rules) · {sub}", "Skipped")]
    assert ctx.src.jev.calls == []


def test_rules_mode_at_threshold_passes(tmp_path):
    # alive 0.70, organic 0.65, chase 0.30, flush 0.75 is impossible in rules (flush is 0/0.5/1);
    # use flush 1 with alive/organic/chase exactly at threshold.
    ctx, an = _analyst(tmp_path, "rules", volumeH1=17500, buysH1=97.5, changeH1=24,
                       changeM5=-5, buysM5=10, sellsM5=10)
    out = run(an.handle(_msg()))
    assert out["answers"] == {"alive_6h": 0.7, "organic": 0.65, "flush": 1.0, "chase": 0.3}
    assert out["passed"] and out["score"] == 76 and out["model"] == "rules"
