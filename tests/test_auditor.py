"""STEP 13 item 5 · Auditor: freeze authority set -> red at layer 2, 3-8 not checked."""

from __future__ import annotations

import bus
from agents.auditor import Auditor, biggest_group, delegation_scan
from tests.fakes import MINT, POOL, add_coin, make_ctx, run

SCAN = {"agent": "scanner", "mint": MINT, "name": "Frog", "pool": POOL, "liquidityUsd": 50000.0}


def test_freeze_authority_red_at_layer_2(tmp_path):
    ctx = make_ctx(tmp_path)
    add_coin(ctx)
    ctx.src.rpc.freeze_authority = "FreezeAuth1111111111111111111111111111111111"
    out = run(Auditor(ctx).audit(SCAN))
    assert out["passed"] is False and out["layersChecked"] == 2 and out["reason"] == "freeze not revoked"
    assert out["layers"][0]["pass"] is True and out["layers"][0]["value"] == "Revoked"
    assert out["layers"][1]["pass"] is False and out["layers"][1]["value"] == "freeze not revoked"
    for l in out["layers"][2:]:
        assert l["value"] == "not checked" and l["pass"] is None
    rows = ctx.conn.execute("SELECT layer, value_text, pass FROM audits ORDER BY layer").fetchall()
    assert [tuple(r) for r in rows] == [(1, "Revoked", 1), (2, "freeze not revoked", 0)] + \
        [(n, "not checked", None) for n in range(3, 9)]
    a = ctx.conn.execute("SELECT title, subtitle, value FROM activity").fetchone()
    assert tuple(a) == ("Rejected Frog", "Auditor · freeze not revoked", "Skipped")
    assert ctx.src.rugcheck.report_calls == 0   # stopped before any later source


def test_clean_coin_8_of_8_to_analyst(tmp_path):
    ctx = make_ctx(tmp_path)
    add_coin(ctx)

    async def go():
        out = await Auditor(ctx).audit(SCAN)
        return out, bus.auditor_to_analyst.get_nowait()
    out, nxt = run(go())
    vals = [l["value"] for l in out["layers"]]
    assert out["passed"] and out["layersChecked"] == 8, vals
    assert vals == ["Revoked", "Revoked", "0.5% · sold 0", "8.7%", "100% burned", "43m", "2 wallets", "None"]
    assert out["holders"] == 41 and nxt["auditor"]["passed"]
    assert ctx.src.rugcheck.report_calls == 1   # fetched once per audit, reused
    events = [e for e, d in bus.history if e == "audit_layer" and "pass" in d]
    assert len(events) == 8


def test_empty_graph_is_zero_bundles_and_the_audit_continues(tmp_path):
    """RugCheck's valid empty answer [] = no insider network, not 'no data'."""
    ctx = make_ctx(tmp_path)
    add_coin(ctx)
    ctx.src.rugcheck.graph_body = []
    out = run(Auditor(ctx).audit(SCAN))
    assert out["passed"] is True and out["layersChecked"] == 8
    assert out["layers"][6] == {"n": 7, "name": "Bundled wallets", "pass": True,
                                "value": "None", "source": "rugcheck"}
    assert out["layers"][7]["pass"] is True
    assert ctx.conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0] == 0


def test_dev_sold_and_dev_holds(tmp_path):
    ctx = make_ctx(tmp_path)
    add_coin(ctx)
    ctx.src.rpc.creator_balance = 4_000_000_000
    assert run(Auditor(ctx).audit(SCAN))["reason"] == "dev sold"
    ctx2 = make_ctx(tmp_path / "x") if (tmp_path / "x").mkdir() is None else None
    add_coin(ctx2, creator_bal=310_000_000_000)
    ctx2.src.rpc.creator_balance = 310_000_000_000
    assert run(Auditor(ctx2).audit(SCAN))["reason"] == "dev holds 31%"


def test_creator_unknown_red_no_data_dev(tmp_path):
    ctx = make_ctx(tmp_path)
    add_coin(ctx, creator=None, creator_bal=None)
    assert run(Auditor(ctx).audit(SCAN))["reason"] == "no data: dev"


def test_pool_too_young(tmp_path):
    ctx = make_ctx(tmp_path)
    add_coin(ctx, migrated_min_ago=5)
    assert run(Auditor(ctx).audit(SCAN))["reason"] == "pool too young"


def test_layer8_down_alerts_and_red(tmp_path):
    ctx = make_ctx(tmp_path)
    add_coin(ctx)
    ctx.src.rpc.gpa_fail = True
    out = run(Auditor(ctx).audit(SCAN))
    assert out["reason"] == "no data: delegations"
    assert ctx.notifier.sent[-1]["text"] == "layer 8 down, every coin is red until rpc_url changes"


def test_drain_found():
    accs = [{"account": {"data": {"parsed": {"info": {"tokenAmount": {"amount": "5"},
             "delegate": "9fQxAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAT2a"}}}}} for _ in range(41)]
    assert delegation_scan(accs)[:2] == (41, "9fQxAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAT2a")


def test_bundles_group_counts_only_holders():
    g = [{"nodes": [{"id": a, "holdings": h} for a, h in
                    [("A", 1), ("B", 1), ("C", 0), ("D", 1), ("E", 1), ("F", 1), ("G", 1)]],
          "links": [{"source": "A", "target": "B"}, {"source": "B", "target": "C"},
                    {"source": "C", "target": "D"}, {"source": "D", "target": "E"},
                    {"source": "E", "target": "F"}, {"source": "F", "target": "G"}]}]
    assert biggest_group(g) == 4     # D-E-F-G ; C holds nothing so A-B is cut off
    assert biggest_group([]) == 0                                 # valid empty: no network
    assert biggest_group([{"nodes": []}]) is None                 # malformed: no links key
    assert biggest_group(None) is None and biggest_group({}) is None
    assert biggest_group("error") is None and biggest_group([{"links": []}]) == 0


def test_rejected_mint_not_reaudited(tmp_path):
    ctx = make_ctx(tmp_path)
    add_coin(ctx)
    ctx.src.rpc.mint_authority = "X"
    a = Auditor(ctx)
    run(a.handle(SCAN))
    assert run(a.handle(SCAN)) is None


def test_top10_falls_back_to_program_accounts_on_429(tmp_path):
    from sources import RateLimited
    ctx = make_ctx(tmp_path)
    add_coin(ctx)

    async def limited(mint):
        raise RateLimited("rpc getTokenLargestAccounts")
    ctx.src.rpc.largest_accounts = limited
    accs = [{"pubkey": "PoolVault", "account": {"data": {"parsed": {"info": {
        "owner": POOL, "tokenAmount": {"amount": "500000000000"}}}}}}]
    accs += [{"pubkey": f"A{i}", "account": {"data": {"parsed": {"info": {
        "owner": f"W{i}", "tokenAmount": {"amount": "10000000000"}}}}}} for i in range(12)]
    ctx.src.rpc.program_accounts_list = accs
    out = run(Auditor(ctx).audit(SCAN))
    assert out["layers"][3]["value"] == "10.0%"     # pool vault excluded, 10 x 1%


def test_graph_error_is_still_no_data_bundles(tmp_path):
    """A failed or missing graph is still red; only a valid empty [] is a pass."""
    from sources import SourceError
    ctx = make_ctx(tmp_path)
    add_coin(ctx)

    async def boom(mint):
        raise SourceError("rugcheck HTTP 500")
    ctx.src.rugcheck.graph = boom
    out = run(Auditor(ctx).audit(SCAN))
    assert out["passed"] is False and out["reason"] == "no data: bundles"
    assert out["layersChecked"] == 7 and out["layers"][7]["value"] == "not checked"

    ctx2 = make_ctx(tmp_path / "malformed")
    add_coin(ctx2)
    ctx2.src.rugcheck.graph_body = [{"nodes": []}]     # dict without a links key
    out2 = run(Auditor(ctx2).audit(SCAN))
    assert out2["passed"] is False and out2["reason"] == "no data: bundles"
