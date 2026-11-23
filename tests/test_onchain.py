"""Nov 23: on-chain backup feed (Kacper: 'Yes, add the on-chain backup feed'). Polls pump.fun's
migration authority over our RPC and feeds the Scanner the same message shape as PumpPortal,
deduped by mint across both feeds."""

from __future__ import annotations

import asyncio
import logging

import pytest

import bus
import config
import sources
from agents.scanner import (MIGRATION_AUTHORITY, ONCHAIN_DOWN_AFTER_S, WSOL, Scanner, alert_text,
                            migrate_mints)
from tests.fakes import MINT, dex_view, make_ctx, run

MINT2 = "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjfpump"
LP = "LPmintXk1Ww3aJ7e1X9yM2hQGGq3n5JqZfX6vYbS4cV"


def _tx(mint, migrate=True, err=None, bt=None):
    logs = ["Program log: Instruction: MigrateV2" if migrate else "Program log: Instruction: Withdraw"]
    return {"blockTime": bt, "meta": {"err": err, "logMessages": logs, "postTokenBalances": [
        {"mint": WSOL}, {"mint": mint}, {"mint": LP}]}}


class Chain:
    """Fake RPC for the authority: newest first, honours 'until', counts calls."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.sigs: list[dict] = []      # newest first
        self.txs: dict[str, dict] = {}
        self.calls: list[str] = []
        self.fail_tx: set[str] = set()

    def add(self, sig, tx, minutes_ago=1, err=None):
        bt = int(self.ctx.now().timestamp()) - minutes_ago * 60
        tx["blockTime"] = bt
        self.sigs.insert(0, {"signature": sig, "blockTime": bt, "err": err})
        self.txs[sig] = tx

    async def call(self, method, params, timeout=20, max_bytes=None):
        self.calls.append(method)
        if method == "getSignaturesForAddress":
            assert params[0] == MIGRATION_AUTHORITY
            out = []
            for r in self.sigs:
                if r["signature"] == params[1].get("until"):
                    break
                out.append(r)
            return out[: params[1].get("limit", 1000)]
        if method == "getTransaction":
            assert params[1]["maxSupportedTransactionVersion"] == 1     # pump.fun now sends v1 tx
            if params[0] in self.fail_tx:
                raise sources.SourceError("rpc getTransaction error -32015")
            return self.txs.get(params[0])
        raise AssertionError(method)


def _setup(tmp_path):
    ctx = make_ctx(tmp_path)
    chain = Chain(ctx)
    ctx.src.rpc.call = chain.call
    return ctx, chain, Scanner(ctx)


def test_migrate_mints_picks_the_graduated_token():
    assert migrate_mints(_tx(MINT)) == [MINT]                      # the pump mint; WSOL / LP never
    assert migrate_mints(_tx("3LJhbS5EJwfJHU5cJ1JYk3fgmC8ajy93Kj56pTi9dcty")) == [
        "3LJhbS5EJwfJHU5cJ1JYk3fgmC8ajy93Kj56pTi9dcty", LP]          # no suffix: curve check decides
    assert migrate_mints(_tx(MINT, migrate=False)) == []          # authority tx that isn't a migration
    assert migrate_mints(_tx(MINT, err={"InstructionError": 1})) == []
    assert migrate_mints(None) == []


def test_settings_exist_with_defaults():
    assert config.desk_default("scanner.onchain_feed") == "on"
    assert config.desk_default("scanner.onchain_poll_s") == 15
    assert config.desk_default("scanner.onchain_safety_poll_s") == 60


def test_scanner_gets_onchain_mints_same_shape_and_source_logged(tmp_path, caplog):
    ctx, chain, s = _setup(tmp_path)
    chain.add("s1", _tx(MINT), minutes_ago=5)
    chain.add("s2", _tx(MINT2), minutes_ago=2)
    chain.add("s3", _tx(MINT2, migrate=False), minutes_ago=1)
    with caplog.at_level(logging.INFO, logger="desk.scanner"):
        assert run(s.onchain_poll_once()) == 2
    assert set(s.room) == {MINT, MINT2} and s.by_source == {"pumpportal": 0, "onchain": 2}
    assert s.room[MINT]["signature"] == "s1"
    lines = [r.getMessage() for r in caplog.records]
    assert f"migration\t{MINT}\tsig=s1\tsource=onchain" in lines
    # migrated_at is the block time, not receive time
    mig = ctx.conn.execute("SELECT migrated_at FROM coins WHERE mint=?", (MINT,)).fetchone()[0]
    assert (ctx.now() - __import__("runtime").parse_iso(mig)).total_seconds() == pytest.approx(300, abs=1)
    # next poll only asks for newer signatures and takes nothing twice
    chain.calls.clear()
    assert run(s.onchain_poll_once()) == 0
    assert chain.calls == ["getSignaturesForAddress"]


def test_first_poll_backfills_only_max_wait_min(tmp_path):
    ctx, chain, s = _setup(tmp_path)
    chain.add("old", _tx(MINT), minutes_ago=90)        # older than max_wait_min (60): can't be decided
    chain.add("new", _tx(MINT2), minutes_ago=30)
    assert run(s.onchain_poll_once()) == 1 and set(s.room) == {MINT2}


def test_dedupe_pumpportal_then_onchain_and_onchain_then_pumpportal(tmp_path):
    ctx, chain, s = _setup(tmp_path)
    pp = {"signature": "pp", "mint": MINT, "txType": "migrate", "pool": "pump-amm"}
    assert run(s.on_migration(pp)) == MINT                        # PumpPortal first
    chain.add("s1", _tx(MINT))
    chain.add("s2", _tx(MINT2))
    assert run(s.onchain_poll_once()) == 1                        # only MINT2 is new
    assert s.dupes == 1 and s.by_source == {"pumpportal": 1, "onchain": 1}
    assert run(s.on_migration({**pp, "mint": MINT2})) is None     # on-chain first, PumpPortal late
    assert s.dupes == 2
    assert ctx.conn.execute("SELECT COUNT(*) FROM coins").fetchone()[0] == 2


def test_dedupe_survives_a_restart_and_a_scanned_coin(tmp_path):
    ctx, chain, s = _setup(tmp_path)
    chain.add("s1", _tx(MINT))
    assert run(s.onchain_poll_once()) == 1
    s2 = Scanner(ctx)                                             # restart: new process, same db
    s2.room.clear()
    assert run(s2.on_migration({"signature": "pp", "mint": MINT, "txType": "migrate", "pool": "pump-amm"})) is None


def test_failed_tx_fetch_is_retried_then_dropped(tmp_path):
    ctx, chain, s = _setup(tmp_path)
    chain.add("s1", _tx(MINT))
    chain.fail_tx.add("s1")
    assert run(s.onchain_poll_once()) == 0 and "s1" in s.onchain_retry
    chain.fail_tx.clear()
    assert run(s.onchain_poll_once()) == 1 and MINT in s.room and not s.onchain_retry
    chain.add("s2", _tx(MINT2))
    chain.fail_tx.add("s2")
    for _ in range(6):
        run(s.onchain_poll_once())
    assert "s2" not in s.onchain_retry and MINT2 not in s.room


def test_onchain_coin_is_scanned_and_passes_to_the_auditor(tmp_path):
    ctx, chain, s = _setup(tmp_path)
    chain.add("s1", _tx(MINT), minutes_ago=0)
    assert run(s.onchain_poll_once()) == 1
    ctx.src.dex.views[MINT] = dex_view(liq=60000, changeH1=10)
    ctx.clock.advance(minutes=11)
    d = run(s.poll_once())
    assert d[0]["verdict"] == "pass"
    assert bus.scanner_to_auditor.get_nowait()["mint"] == MINT


def test_backup_polls_fast_only_when_pumpportal_is_down(tmp_path):
    ctx, chain, s = _setup(tmp_path)
    t0 = s.ws_lost_at
    assert not s.pumpportal_down(t0 + 10) and s.pumpportal_down(t0 + ONCHAIN_DOWN_AFTER_S)
    s.on_open()
    assert not s.pumpportal_down(t0 + 10_000)                     # connected: safety-net pace
    run(s.on_reconnect("real", "InvalidStatus 502", 60))
    lost = s.ws_lost_at
    assert not s.pumpportal_down(lost + 60) and s.pumpportal_down(lost + 121)


def test_onchain_loop_paces_and_survives_rpc_errors(tmp_path, monkeypatch):
    ctx, chain, s = _setup(tmp_path)
    sleeps = []

    async def boom(*a, **k):
        raise sources.SourceError("rpc down")
    ctx.src.rpc.call = boom

    async def fake_sleep(sec):
        sleeps.append(sec)
        if len(sleeps) == 1:
            s.on_open()                                            # PumpPortal back -> slow pace
        if len(sleeps) >= 2:
            raise asyncio.CancelledError
    monkeypatch.setattr("agents.scanner.asyncio.sleep", fake_sleep)
    s.ws_lost_at -= 600                                            # down for 10 min
    with pytest.raises(asyncio.CancelledError):
        run(s.onchain_loop())
    assert sleeps == [15, 60]


def test_feed_off_does_no_rpc(tmp_path, monkeypatch):
    ctx, chain, s = _setup(tmp_path)
    config.set_value(ctx.conn, "scanner.onchain_feed", "off")

    async def fake_sleep(sec):
        raise asyncio.CancelledError
    monkeypatch.setattr("agents.scanner.asyncio.sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        run(s.onchain_loop())
    assert chain.calls == [] and s.backup_last_hour(0) is None


def test_feed_down_alert_says_backup_is_bringing_coins(tmp_path):
    import time
    now = time.time()
    t = alert_text(30, now - 3600, "InvalidStatus 502", now, 4)
    assert t.endswith("(PumpPortal answered HTTP 502), backup on-chain feed is active (4 coins in the last hour), coins still coming in")
    assert alert_text(30, now - 3600, "InvalidStatus 502", now, 1).endswith("(1 coin in the last hour), coins still coming in")
    assert alert_text(30, now - 3600, "InvalidStatus 502", now, None).endswith("no new coins until it reconnects")
    ctx, chain, s = _setup(tmp_path)
    chain.add("s1", _tx(MINT))
    run(s.onchain_poll_once())
    assert s.backup_last_hour(time.time()) == 1
