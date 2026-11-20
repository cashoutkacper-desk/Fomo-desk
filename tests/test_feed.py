"""Nov 19-20: OOM loop from a non-pumpfun 'migration' (PUMP token) + PumpPortal refusing
connections for hours. Scanner intake filter, RPC response cap, feed-down alert text."""

from __future__ import annotations

import json
import time

import pytest

import bus
import sources
from agents.scanner import PUMP_PROGRAM, Scanner, alert_text
from tests.fakes import MINT, make_ctx, run

PUMP_TOKEN = "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn"     # PUMP itself: no 'pump' suffix, no curve


def _mig(mint):
    return {"signature": "sig", "mint": mint, "txType": "migrate", "pool": "pump-amm"}


def test_non_pumpfun_pool_is_ignored(tmp_path):
    ctx = make_ctx(tmp_path)

    async def no_curve(pubkey):
        return {"result": {"value": None}}
    ctx.src.rpc.account_info = no_curve
    s = Scanner(ctx)
    assert run(s.on_migration(_mig(PUMP_TOKEN))) is None
    assert PUMP_TOKEN not in s.room and s.ignored == 1
    assert ctx.conn.execute("SELECT COUNT(*) FROM coins WHERE mint=?", (PUMP_TOKEN,)).fetchone()[0] == 0


def test_account_owned_by_another_program_is_not_a_curve(tmp_path):
    ctx = make_ctx(tmp_path)          # FakeRpc.account_info answers with a Token-program owner
    assert run(Scanner(ctx).on_migration(_mig("XsbEhLAtcf6HdfpFZ5xEMdqW8nfAvcsP5bdudRLJzJp"))) is None


def test_non_suffix_mint_with_a_pumpfun_curve_is_kept(tmp_path):
    ctx = make_ctx(tmp_path)
    m = "3LJhbS5EJwfJHU5cJ1JYk3fgmC8ajy93Kj56pTi9dcty"

    async def curve(pubkey):
        return {"result": {"value": {"owner": PUMP_PROGRAM, "data": ["", "base64"]}}}
    ctx.src.rpc.account_info = curve
    assert run(Scanner(ctx).on_migration(_mig(m))) == m


def test_rpc_down_lets_a_valid_mint_in_and_garbage_out(tmp_path):
    ctx = make_ctx(tmp_path)

    async def down(pubkey):
        raise sources.SourceError("rpc down")
    ctx.src.rpc.account_info = down
    s = Scanner(ctx)
    assert run(s.on_migration(_mig("XsbEhLAtcf6HdfpFZ5xEMdqW8nfAvcsP5bdudRLJzJp"))) is not None
    assert run(s.on_migration(_mig("Not0Base58Il"))) is None


def test_pump_suffix_needs_no_rpc_call(tmp_path):
    ctx = make_ctx(tmp_path)

    async def boom(pubkey):
        raise AssertionError("no RPC call for a ...pump mint")
    ctx.src.rpc.account_info = boom
    assert run(Scanner(ctx).on_migration(_mig(MINT))) == MINT


class _Stream:
    def __init__(self, chunks, status=200):
        self.chunks, self.status_code = chunks, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aiter_bytes(self):
        for c in self.chunks:
            yield c


class _Client:
    def __init__(self, chunks, status=200):
        self.chunks, self.status = chunks, status

    def stream(self, method, url, **kw):
        return _Stream(self.chunks, self.status)


def test_rpc_refuses_an_oversized_program_accounts_response():
    rpc = sources.Rpc(_Client([b"x" * 1_000_000] * 20), lambda: "https://rpc.example")
    with pytest.raises(sources.SourceError, match="too many accounts"):
        run(rpc.program_accounts(MINT))


def test_rpc_small_program_accounts_response_parses():
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": [{"pubkey": "A"}]}).encode()
    rpc = sources.Rpc(_Client([body[:10], body[10:]]), lambda: "https://rpc.example")
    assert run(rpc.program_accounts(MINT)) == [{"pubkey": "A"}, {"pubkey": "A"}]   # Token + Token-2022


def test_rpc_capped_429_is_rate_limited():
    rpc = sources.Rpc(_Client([], status=429), lambda: "https://rpc.example")
    with pytest.raises(sources.RateLimited):
        run(rpc.program_accounts(MINT))


def test_alert_text_plain_when_flapping_and_with_down_since_when_refused():
    now = time.time()
    assert alert_text(11, None, None, now) == "Websocket dropped 11 times in the last hour (PumpPortal)"
    assert alert_text(11, now - 60, "ConnectionClosedError", now) == "Websocket dropped 11 times in the last hour (PumpPortal)"
    t = alert_text(60, now - 3600, "ConnectionRefusedError", now)
    assert t.startswith("Websocket dropped 60 times in the last hour (PumpPortal) · feed down since ")
    assert t.endswith("(connection refused), no new coins until it reconnects")


def test_scanner_tracks_down_since_and_clears_it_on_open(tmp_path):
    ctx = make_ctx(tmp_path)
    s = Scanner(ctx)

    async def go():
        for i in range(11):
            await s.on_reconnect("real", "ConnectionRefusedError", 60)
        return s.down_since
    since = run(go())
    assert since is not None and s.last_error == "ConnectionRefusedError"
    assert ctx.notifier.sent[-1]["text"].startswith("Websocket dropped 11 times in the last hour (PumpPortal)")
    s.on_open()
    assert s.down_since is None and s.last_open is not None
    run(s.on_reconnect("quiet", "idle", 0))
    assert s.down_since is None                                    # idle reconnects are not a drop
