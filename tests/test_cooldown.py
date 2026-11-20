"""Kacper, Nov 20: quiet reconnect 10 min (ws_silence_s 600, a setting) and a 15 min
cooldown after 10 connection refusals in a row; reset on a good connect."""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

import config
from agents.scanner import Scanner
from tests.fakes import make_ctx, run


def test_settings_exist_with_kacpers_numbers(tmp_path):
    ctx = make_ctx(tmp_path)
    cfg = config.load(ctx.conn)
    assert cfg["scanner.refused_cooldown_after"] == 10 and cfg["scanner.refused_cooldown_min"] == 15
    r = config.set_value(ctx.conn, "ws_silence_s", 600, who="test")
    assert config.load(ctx.conn)["scanner.ws_silence_s"] == 600 and r["new"] in (600, "600")
    row = ctx.conn.execute("SELECT old, new, who FROM change_log WHERE key='scanner.ws_silence_s'").fetchone()
    assert row["new"] == "600" and row["who"] == "test"
    with pytest.raises(config.SettingError):
        config.set_value(ctx.conn, "refused_cooldown_min", 0)


def test_cooldown_after_10_refusals_then_normal_backoff(tmp_path):
    s = Scanner(make_ctx(tmp_path))
    waits = [s.next_wait(ConnectionRefusedError(), b) for b in (1, 2, 4, 8, 16, 32, 60, 60, 60, 60)]
    assert waits[:9] == [1, 2, 4, 8, 16, 32, 60, 60, 60] and waits[9] == 900
    assert s.refused_streak == 0 and s.cooldowns == 1
    assert s.next_wait(ConnectionRefusedError(), 60) == 60          # normal backoff again


def test_good_connect_or_other_error_resets_the_streak(tmp_path):
    s = Scanner(make_ctx(tmp_path))
    for _ in range(9):
        s.next_wait(ConnectionRefusedError(), 60)
    s.on_open()
    assert s.refused_streak == 0
    for _ in range(9):
        s.next_wait(ConnectionRefusedError(), 60)
    assert s.next_wait(TimeoutError(), 60) == 60 and s.refused_streak == 0   # not "in a row" any more


def test_settings_are_live(tmp_path):
    ctx = make_ctx(tmp_path)
    config.set_value(ctx.conn, "refused_cooldown_after", 3, who="test")
    config.set_value(ctx.conn, "refused_cooldown_min", 20, who="test")
    s = Scanner(ctx)
    assert [s.next_wait(ConnectionRefusedError(), 1) for _ in range(3)] == [1, 1, 1200]


def test_ws_loop_cools_down_and_keeps_counting_drops(tmp_path, monkeypatch):
    """The real loop: PumpPortal refuses every connect. 10 refusals -> one 15 min sleep;
    every refusal is still a real drop (hourly count + 'Websocket dropped' alert >10)."""
    ctx = make_ctx(tmp_path)
    s = Scanner(ctx)
    fake = types.ModuleType("websockets")

    def connect(*a, **k):
        raise ConnectionRefusedError(111, "Connection refused")
    fake.connect = connect
    monkeypatch.setitem(sys.modules, "websockets", fake)
    sleeps = []

    async def fake_sleep(sec):
        sleeps.append(sec)
        if len(sleeps) >= 22:
            raise asyncio.CancelledError
    monkeypatch.setattr("agents.scanner.asyncio.sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        run(s.ws_loop())
    assert sleeps[:10] == [1, 2, 4, 8, 16, 32, 60, 60, 60, 900]
    assert sleeps[10:20] == [60] * 9 + [900]                      # after the cooldown: backoff, not reset to 1
    assert s.cooldowns == 2 and s.tracker.real == 22
    alerts = [m["text"] for m in ctx.notifier.sent if m["kind"] == "alert"]
    assert len(alerts) == 1 and alerts[0].startswith("Websocket dropped 11 times in the last hour (PumpPortal)")
