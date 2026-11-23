"""STEP 12 · server side of the 2 routines: desk-health and desk-night-report."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

import health
import night_report
from links import SIGNUP_URL, coin_url
from runtime import iso
from telegram import Desk
from tests.fakes import MINT, add_coin, make_ctx, run

NOW = datetime(2026, 11, 20, 13, 5, tzinfo=timezone.utc)          # 8:05 AM EST, just after report_time
TOAD = "ToadMint111111111111111111111111111111pump"


def _beats(conn, now, ages: dict[str, float]):
    for a, age in ages.items():
        conn.execute("INSERT OR REPLACE INTO heartbeats(agent, ts) VALUES (?,?)",
                     (a, iso(now - timedelta(seconds=age))))
    conn.commit()


ALL_FRESH = {a: 10 for a in ("process", *health.AGENTS)}


async def _nosleep(_):
    return None


def _health(ctx, tmp_path, *, active=True, after_restart=None, now=NOW):
    calls = []

    def restart():
        calls.append("restart")
        if after_restart:
            _beats(ctx.conn, now + timedelta(seconds=30), after_restart)
        return True
    t = iter([now] + [now + timedelta(seconds=40)] * 50)
    res = run(health.run(ctx.conn, ctx.notifier, tmp_path, now=lambda: next(t), now_local=lambda: now.replace(tzinfo=None),
                         is_active=lambda: active, restart=restart, sleep=_nosleep, wait_s=10, step_s=1))
    return res, calls


def test_healthy_does_nothing(tmp_path):
    ctx = make_ctx(tmp_path)
    _beats(ctx.conn, NOW, ALL_FRESH)
    res, calls = _health(ctx, tmp_path)
    assert res == {"ok": True, "restarted": [], "down_min": 0, "reconnects_1h": 0, "alerted": False,
                   "already_alerted": False, "feed_down_since": None}
    assert calls == [] and ctx.notifier.sent == []


def test_stale_agent_restarts_and_sends_exact_text(tmp_path):
    ctx = make_ctx(tmp_path)
    _beats(ctx.conn, NOW, {**ALL_FRESH, "auditor": 7 * 60 + 20})       # 7.3 min -> "7"
    res, calls = _health(ctx, tmp_path, after_restart=ALL_FRESH)
    assert calls == ["restart"]
    assert res["ok"] is True and res["restarted"] == ["auditor"] and res["down_min"] == 7
    assert [m["text"] for m in ctx.notifier.sent] == ["Restarted auditor, down 7 min"]


def test_process_down_is_reported_as_desk(tmp_path):
    ctx = make_ctx(tmp_path)
    _beats(ctx.conn, NOW, {a: 138 * 60 for a in ALL_FRESH})              # whole box asleep 2h18m
    res, calls = _health(ctx, tmp_path, after_restart=ALL_FRESH)
    assert res["restarted"] == ["desk"] and res["down_min"] == 138 and res["ok"]
    assert [m["text"] for m in ctx.notifier.sent] == ["Restarted desk, down 138 min"]


def test_service_inactive_restarts_even_with_fresh_rows(tmp_path):
    ctx = make_ctx(tmp_path)
    _beats(ctx.conn, NOW, {a: 30 for a in ALL_FRESH})
    res, calls = _health(ctx, tmp_path, active=False, after_restart=ALL_FRESH)
    assert calls == ["restart"] and ctx.notifier.sent[-1]["text"] == "Restarted desk, down 0 min"


def test_still_stale_after_restart_is_not_ok(tmp_path):
    ctx = make_ctx(tmp_path)
    _beats(ctx.conn, NOW, {**ALL_FRESH, "risk": 600})
    res, _ = _health(ctx, tmp_path, after_restart=None)
    assert res["ok"] is False and res["restarted"] == ["risk"]
    assert ctx.notifier.sent[-1]["text"] == "Restarted risk, down 10 min"


def _scanner_log(tmp_path, now_local, real: int, quiet: int, old: int = 0):
    lines = []
    for i in range(real):
        t = now_local - timedelta(minutes=2 + i * 4)
        lines.append(f"{t:%Y-%m-%d %H:%M:%S},000\tws_disconnect\t{i + 1}\tConnectionClosedError\tbackoff=2s\tlast_hour={i + 1}")
        lines.append(f"{t:%Y-%m-%d %H:%M:%S},500\tws_open")          # flapping, but it reconnects
    for i in range(quiet):
        t = now_local - timedelta(minutes=1 + i * 2)
        lines.append(f"{t:%Y-%m-%d %H:%M:%S},000\tws_quiet_reconnect\t{i + 1}\tidle 120s")
    for i in range(old):
        t = now_local - timedelta(minutes=70 + i)
        lines.append(f"{t:%Y-%m-%d %H:%M:%S},000\tws_disconnect\t{i}\tX\tbackoff=1s\tlast_hour=1")
    (tmp_path / "scanner.log").write_text("\n".join(sorted(lines)) + "\n")


def test_quiet_reconnects_never_count(tmp_path):
    nl = NOW.replace(tzinfo=None)
    _scanner_log(tmp_path, nl, real=3, quiet=25, old=9)
    assert health.count_reconnects(tmp_path, nl) == 3


def test_more_than_10_real_reconnects_alerts_once(tmp_path):
    ctx = make_ctx(tmp_path)
    _beats(ctx.conn, NOW, ALL_FRESH)
    _scanner_log(tmp_path, NOW.replace(tzinfo=None), real=11, quiet=40)
    res, _ = _health(ctx, tmp_path)
    assert res["reconnects_1h"] == 11 and res["alerted"] is True and res["restarted"] == []
    assert [m["text"] for m in ctx.notifier.sent] == ["Websocket dropped 11 times in the last hour (PumpPortal)"]


def test_10_real_reconnects_is_not_more_than_10(tmp_path):
    ctx = make_ctx(tmp_path)
    _beats(ctx.conn, NOW, ALL_FRESH)
    _scanner_log(tmp_path, NOW.replace(tzinfo=None), real=10, quiet=40)
    res, _ = _health(ctx, tmp_path)
    assert res["reconnects_1h"] == 10 and res["alerted"] is False and ctx.notifier.sent == []


def test_alert_deduplicated_when_main_already_sent_it_this_hour(tmp_path):
    ctx = make_ctx(tmp_path)
    _beats(ctx.conn, NOW, ALL_FRESH)
    _scanner_log(tmp_path, NOW.replace(tzinfo=None), real=12, quiet=0)
    rec = {"ts": iso(NOW - timedelta(minutes=20)), "kind": "alert", "text": "Websocket dropped 11 times in the last hour (PumpPortal)"}
    (tmp_path / "notifier.log").write_text(f"2026-11-20 07:45:00,000\t{json.dumps(rec)}\n")
    res, _ = _health(ctx, tmp_path)
    assert res["reconnects_1h"] == 12 and res["alerted"] is False and ctx.notifier.sent == []
    assert res["already_alerted"] is True
    # an alert older than an hour does not block a new one
    rec["ts"] = iso(NOW - timedelta(minutes=61))
    (tmp_path / "notifier.log").write_text(f"2026-11-20 07:04:00,000\t{json.dumps(rec)}\n")
    res, _ = _health(ctx, tmp_path)
    assert res["alerted"] is True


# ------------------------------------------------------------------ night report

def _seed_night(ctx):
    c = ctx.conn
    add_coin(ctx)
    add_coin(ctx, mint=TOAD, name="Toad")
    t = lambda h: iso(NOW - timedelta(hours=h))  # noqa: E731
    c.execute("INSERT INTO balance(ts, value) VALUES (?, 200)", (t(30),))
    c.execute("INSERT INTO balance(ts, value) VALUES (?, 198.27)", (t(1),))
    # inside the night (yesterday 8:00 AM EST .. now)
    c.execute("INSERT INTO positions(mint, opened_at, entry_price, size_usd, peak_price, stop_price, trail_active,"
              " closed_at, exit_price, exit_reason, pnl_usd, fees_usd, mode) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (MINT, t(5), 0.001, 47.5, 0.001, 0.00094, 0, t(4), 0.00093, "stop", -5.16, 1.83, "PAPER"))
    c.execute("INSERT INTO positions(mint, opened_at, entry_price, size_usd, peak_price, stop_price, trail_active,"
              " closed_at, exit_price, exit_reason, pnl_usd, fees_usd, mode) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (TOAD, t(3), 0.002, 47.5, 0.0026, 0.00229, 1, t(2), 0.00229, "trail", 3.43, 1.90, "PAPER"))
    # outside the night (before yesterday 8:00 AM EST)
    c.execute("INSERT INTO positions(mint, opened_at, entry_price, size_usd, closed_at, exit_price, exit_reason,"
              " pnl_usd, fees_usd, mode) VALUES (?,?,?,?,?,?,?,?,?,?)",
              (TOAD, t(27), 0.002, 47.5, t(26), 0.003, "trail", 20, 1.9, "PAPER"))
    for mint, reason, h in ((MINT, None, 6), (TOAD, "liq $3.1K", 6), (TOAD, "liq $9", 5),
                            ("Copy111111111111111111111111111111111pump", "copycat of Frog", 5)):
        if mint.startswith("Copy"):
            add_coin(ctx, mint=mint, name="Frogg")
        c.execute("INSERT INTO scans(mint, ts, verdict, reason) VALUES (?,?,?,?)",
                  (mint, t(h), "kill" if reason else "pass", reason))
    c.execute("INSERT INTO audits(mint, ts, layer, name, value_text, pass, source) VALUES (?,?,?,?,?,?,?)",
              (TOAD, t(4.5), 3, "Top 10 holders", "41%", 0, "rpc"))
    c.execute("INSERT INTO verdicts(mint, ts, score, pass, reason, input_tokens) VALUES (?,?,?,?,?,?)",
              (MINT, t(5.5), 64, 0, "chasing", 812))
    c.execute("INSERT INTO verdicts(mint, ts, score, pass, reason, input_tokens) VALUES (?,?,?,?,?,?)",
              (TOAD, t(3.5), 82, 1, None, 790))
    c.commit()


def _urls(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and v.startswith("http"):
                yield k, v
            else:
                yield from _urls(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _urls(v)


def test_export_shape_order_and_links(tmp_path):
    ctx = make_ctx(tmp_path)
    _seed_night(ctx)
    r = night_report.export(ctx.conn, now=NOW)
    assert r["mode"] == "PAPER" and r["night"]["start"] == "2026-11-19T13:00:00.000Z"
    assert r["balance"] == {"start": 200.0, "end": 198.27}
    assert [t["name"] for t in r["trades"]["red"]] == ["Frog"] and [t["name"] for t in r["trades"]["green"]] == ["Toad"]
    red = r["trades"]["red"][0]
    assert (red["entry"], red["exit"], red["pnl_usd"], red["pnl_pct"], red["rule"]) == (0.001, 0.00093, -5.16, -10.86, "stop")
    assert red["opened_at"] and red["closed_at"] and red["symbol"] == "FROG"
    assert r["trades"]["count"] == 2 and r["fees_usd"] == 3.73      # the 20-$ trade from 2 nights ago is out
    assert r["coins_seen"] == 3
    kills = {g["reason"]: g["count"] for g in r["scanner_kills"]["by_reason"]}
    assert kills == {"liquidity too low": 2, "copycat": 1}
    (lay,) = r["auditor_refusals"]["by_layer"]
    assert lay["layer"] == 3 and lay["coins"][0]["name"] == "Toad" and lay["coins"][0]["ts"]
    assert r["analyst_rejects"]["items"][0]["failed_rule"] == "chasing"
    assert r["jev"]["input_tokens"] == 1602 and r["jev"]["calls"] == 2 and r["jev"]["cost_usd"] is None
    assert r["footer"] == "New to FOMO? " + SIGNUP_URL and r["signup_url"] == SIGNUP_URL
    # every coin entry anywhere carries its links.py coin_url, and every FOMO URL has savipww
    def coin_entries(o):
        if isinstance(o, dict):
            if "mint" in o:
                yield o
            for v in o.values():
                yield from coin_entries(v)
        elif isinstance(o, list):
            for v in o:
                yield from coin_entries(v)
    entries = list(coin_entries(r))
    assert entries and all(e["coin_url"] == coin_url(e["mint"], "solana") for e in entries)
    urls = [u for _, u in _urls(r)]
    assert urls and all("fomo.family" in u and ("r=savipww" in u or u == SIGNUP_URL) for u in urls)
    assert {c["mint"] for c in r["coins"]} >= {MINT, TOAD}
    json.dumps(r)


def test_export_has_no_secrets_or_tokens(tmp_path):
    ctx = make_ctx(tmp_path)
    _seed_night(ctx)
    s = json.dumps(night_report.export(ctx.conn, now=NOW)).lower()
    for bad in ("token\":", "app_token", "data_path", "bearer", "/d/"):
        assert bad not in s


def test_export_empty_night(tmp_path):
    ctx = make_ctx(tmp_path)
    r = night_report.export(ctx.conn, now=NOW)
    assert r["empty"] is True and r["trades"] == {"count": 0, "red": [], "green": [], "open": []}
    assert r["coins_seen"] == 0 and r["scanner_kills"]["count"] == 0 and r["fees_usd"] == 0
    assert r["balance"]["start"] == r["balance"]["end"] == 200.0 and r["footer"].endswith(SIGNUP_URL)


def test_late_run_still_covers_the_whole_night(tmp_path):
    ctx = make_ctx(tmp_path)
    s, _ = night_report.night_window(__import__("config").load(ctx.conn), NOW + timedelta(hours=5))
    assert s == "2026-11-19T13:00:00.000Z"


PAGE = "https://pages.example.com/r/abc123"


def _skips(ctx):
    add_coin(ctx)
    ctx.conn.execute("INSERT INTO skips(mint, until) VALUES (?,?)", (MINT, iso(NOW + timedelta(hours=3))))
    ctx.conn.commit()


def test_finalize_stores_sends_then_clears_skips(tmp_path):
    ctx = make_ctx(tmp_path)
    _skips(ctx)
    order = []
    orig = ctx.notifier.message

    async def spy(text, **kw):
        order.append(("send", ctx.conn.execute("SELECT COUNT(*) FROM skips").fetchone()[0],
                      ctx.conn.execute("SELECT value FROM settings WHERE key='last_report_url'").fetchone()[0]))
        return await orig(text, **kw)
    ctx.notifier.message = spy
    res = run(night_report.finalize(ctx.conn, ctx.notifier, PAGE))
    assert res == {"ok": True, "stored": True, "sent": True, "skips_cleared": 1}
    assert order == [("send", 1, PAGE)]                                 # stored before, skips still there at send
    assert ctx.notifier.sent[-1]["text"] == f"Morning report: {PAGE}"
    assert ctx.conn.execute("SELECT COUNT(*) FROM skips").fetchone()[0] == 0


def test_finalize_keeps_skips_when_telegram_fails(tmp_path):
    ctx = make_ctx(tmp_path)
    _skips(ctx)
    ctx.notifier.fail_times = 5
    res = run(night_report.finalize(ctx.conn, ctx.notifier, PAGE))
    assert res["sent"] is False and res["skips_cleared"] == 0 and res["ok"] is False
    assert ctx.conn.execute("SELECT COUNT(*) FROM skips").fetchone()[0] == 1


@pytest.mark.parametrize("bad", ["http://pages.example.com/x", "pages.example.com/x", "", "https://", "javascript:alert(1)",
                                 "https://fomo.family/coin?address=X&chainId=1399811149"])
def test_finalize_validates_url(tmp_path, bad):
    ctx = make_ctx(tmp_path)
    _skips(ctx)
    with pytest.raises(ValueError):
        run(night_report.finalize(ctx.conn, ctx.notifier, bad))
    assert ctx.notifier.sent == [] and ctx.conn.execute("SELECT COUNT(*) FROM skips").fetchone()[0] == 1


def test_report_command_returns_the_url(tmp_path):
    ctx = make_ctx(tmp_path)
    run(night_report.finalize(ctx.conn, ctx.notifier, PAGE))
    replies = run(Desk(ctx.conn).handle("report"))
    assert replies[0]["text"] == f"Last morning Page:\n{PAGE}"


# ------------------------------------------------------------------ Nov 20: feed refused for hours

def _refused_log(tmp_path, now_local, minutes=70):
    """Like 04:06-08:30 AM Nov 20: one ConnectionClosed, then refused every 60 s, no ws_open."""
    start = now_local - timedelta(minutes=minutes)
    lines = [f"{start - timedelta(minutes=2):%Y-%m-%d %H:%M:%S},000\tws_open",
             f"{start:%Y-%m-%d %H:%M:%S},000\tws_disconnect\t1\tConnectionClosedError\tbackoff=1s\tlast_hour=1"]
    for i in range(1, minutes):
        t = start + timedelta(minutes=i)
        lines.append(f"{t:%Y-%m-%d %H:%M:%S},000\tws_disconnect\t{i + 1}\tConnectionRefusedError\tbackoff=60s\tlast_hour=60")
    (tmp_path / "scanner.log").write_text("\n".join(lines) + "\n")
    return start


def test_feed_refused_for_an_hour_alerts_with_down_since(tmp_path):
    ctx = make_ctx(tmp_path)
    _beats(ctx.conn, NOW, ALL_FRESH)
    nl = NOW.replace(tzinfo=None)
    start = _refused_log(tmp_path, nl)
    res, _ = _health(ctx, tmp_path)
    assert res["reconnects_1h"] == 60 and res["alerted"] is True and res["already_alerted"] is False
    assert res["feed_down_since"] == f"{start:%Y-%m-%d %H:%M}"
    assert ctx.notifier.sent[-1]["text"] == (f"Websocket dropped 60 times in the last hour (PumpPortal) · feed down since "
                                             f"{start:%-I:%M %p} (connection refused), no new coins until it reconnects")


def test_reconnects_60_with_desk_alert_this_hour_reports_already_alerted(tmp_path):
    """What happened overnight: reconnects_1h=60, alerted=false because main.py's Scanner had
    already sent 'Websocket dropped ...' that hour (05:11, 06:11, 07:11, 08:11 AM)."""
    ctx = make_ctx(tmp_path)
    _beats(ctx.conn, NOW, ALL_FRESH)
    _refused_log(tmp_path, NOW.replace(tzinfo=None))
    rec = {"ts": iso(NOW - timedelta(minutes=54)), "kind": "alert",
           "text": "Websocket dropped 60 times in the last hour (PumpPortal)"}
    (tmp_path / "notifier.log").write_text(f"2026-11-20 07:11:00,000\t{json.dumps(rec)}\n")
    res, _ = _health(ctx, tmp_path)
    assert res["alerted"] is False and res["already_alerted"] is True and res["feed_down_since"] is not None
    assert ctx.notifier.sent == []


def test_quiet_reconnects_all_night_are_not_drops_and_feed_is_up(tmp_path):
    nl = NOW.replace(tzinfo=None)
    lines = []
    for i in range(30):                                   # idle 120 s reconnects, every 2 min
        t = nl - timedelta(minutes=60 - 2 * i)
        lines += [f"{t:%Y-%m-%d %H:%M:%S},000\tws_quiet_reconnect\t{i}\tidle 120s", f"{t:%Y-%m-%d %H:%M:%S},050\tws_open"]
    (tmp_path / "scanner.log").write_text("\n".join(lines) + "\n")
    assert health.count_reconnects(tmp_path, nl) == 0
    assert health.feed_state(tmp_path, nl)["down"] is False


def test_health_alert_names_the_http_status(tmp_path):
    ctx = make_ctx(tmp_path)
    _beats(ctx.conn, NOW, ALL_FRESH)
    nl = NOW.replace(tzinfo=None)
    start = nl - timedelta(minutes=70)
    lines = [f"{start - timedelta(minutes=1):%Y-%m-%d %H:%M:%S},000\tws_open"]
    for i in range(70):
        t = start + timedelta(minutes=i)
        lines.append(f"{t:%Y-%m-%d %H:%M:%S},000\tws_disconnect\t{i + 1}\tInvalidStatus 502\tbackoff=60s\tlast_hour=60")
    (tmp_path / "scanner.log").write_text("\n".join(lines) + "\n")
    res, _ = _health(ctx, tmp_path)
    assert res["alerted"] and res["feed_down_since"] == f"{start:%Y-%m-%d %H:%M}"
    assert ctx.notifier.sent[-1]["text"].endswith(
        f"feed down since {start:%-I:%M %p} (PumpPortal answered HTTP 502), no new coins until it reconnects")
