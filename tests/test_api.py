"""STEP 7 · API contract (plan A server, same shapes as plan B files)."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest
from fastapi.testclient import TestClient

import bus
import config
from api import create_app
from links import SIGNUP_URL, coin_url, profile_url
from runtime import iso
from tests.fakes import MINT, add_coin, make_ctx

TOKEN = "t" * 48 + "-fake-app-token"
GETS = ["/api/state", "/api/chart?range=1H", "/api/activity", f"/api/coin/{MINT}", f"/api/audit/{MINT}",
        "/api/agents/scanner/decisions", "/api/account", "/api/settings", "/api/stream"]
POSTS = [("/api/settings", {"key": "stop", "value": 7}), ("/api/agents/scanner/pause", {}),
         ("/api/agents/scanner/resume", {}), ("/api/mode", {"mode": "PAPER"}),
         ("/api/push", {"endpoint": "https://push.example/x", "keys": {"p256dh": "a", "auth": "b"}})]


def seed(ctx, n_activity=0):
    c = ctx.conn
    add_coin(ctx)
    now = ctx.now()
    c.execute("UPDATE settings SET value='@Cashoutkacper' WHERE key='fomo_handle'") or \
        c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('fomo_handle','@Cashoutkacper')")
    c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('fomo_handle','@Cashoutkacper')")
    for i, (p, v) in enumerate([(1.0, 100), (1.2, 150), (0.9, 180), (1.1, 200)]):
        c.execute("INSERT INTO prices(mint, ts, price, volume_m5, writer) VALUES (?,?,?,?, 'scanner')",
                  (MINT, iso(now - timedelta(seconds=90 - i * 20)), p, v))
    for n, (val, ok) in enumerate([("Revoked", 1), ("Revoked", 1), ("0.9% · sold 0", 1), ("16.8%", 1),
                                   ("100% burned", 1), ("43m", 1), ("None", 1), ("None", 1)], 1):
        c.execute("INSERT INTO audits(mint, ts, layer, name, value_text, pass, source) VALUES (?,?,?,?,?,?,?)",
                  (MINT, iso(now), n, "x", val, ok, "rpc"))
    c.execute("INSERT INTO balance(ts, value) VALUES (?, 200)", (iso(now - timedelta(hours=1)),))
    for i in range(n_activity):
        c.execute("INSERT INTO activity(ts, kind, mint, agent, title, subtitle, value) VALUES (?,?,?,?,?,?,?)",
                  (iso(now), "rejected" if i % 2 else "bought", MINT, "auditor", f"Row {i}", "s", "v"))
    c.commit()


@pytest.fixture
def env(tmp_path):
    ctx = make_ctx(tmp_path)
    seed(ctx, n_activity=65)
    app = create_app(conn_getter=lambda: ctx.conn, token_getter=lambda: TOKEN, serve_web=False)
    return ctx, TestClient(app)


H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.mark.parametrize("path", GETS)
def test_every_get_needs_token(env, path):
    _, cl = env
    assert cl.get(path).status_code == 401
    assert cl.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401


@pytest.mark.parametrize("path, body", POSTS)
def test_every_post_needs_token(env, path, body):
    _, cl = env
    assert cl.post(path, json=body).status_code == 401


def test_health_open(env):
    assert env[1].get("/health").json() == {"status": "ok"}


def test_state(env):
    ctx, cl = env
    r = cl.get("/api/state", headers={"X-App-Token": TOKEN})
    assert r.status_code == 200
    s = r.json()
    assert s["mode"] == "PAPER" and s["balance"] == 200 and s["multiplier"] == 1.0 and s["todayPct"] == 0
    assert [a["name"] for a in s["agents"]] == ["Scanner", "Auditor", "Analyst", "Executor", "Risk"]
    risk = s["agents"][4]
    assert risk["pills"] == [{"text": "stop -6%", "color": "red"}, {"text": "trail 12%", "color": "green"}]
    aud = s["agents"][1]
    assert aud["headline"] == "1 taken apart" and aud["result"] == "8/8 → Executor" and len(aud["layers"]) == 8


def test_chart_ranges_and_marks(env):
    ctx, cl = env
    c = ctx.conn
    now = datetime.now(timezone.utc)   # the API charts against the real clock
    c.execute("INSERT INTO positions(mint, opened_at, entry_price, size_usd, peak_price, stop_price, trail_active,"
              " closed_at, exit_price, exit_reason, pnl_usd, fees_usd, mode) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (MINT, iso(now - timedelta(minutes=10)), 1.0, 47.5, 1.0, 0.92, 0, iso(now - timedelta(minutes=5)),
               0.919, "stop", -5.75, 1.9, "PAPER"))
    c.commit()
    for rng in ("LIVE", "1H", "4H", "1D", "1W", "ALL"):
        assert cl.get(f"/api/chart?range={rng}", headers=H).status_code == 200
    assert cl.get("/api/chart?range=2H", headers=H).status_code == 400
    marks = cl.get("/api/chart?range=1H", headers=H).json()["marks"]
    assert [m["text"] for m in marks] == ["Bought Frog", "-8% · -$5.75"]
    assert marks[0]["color"] == "blue" and marks[1]["color"] == "red"


def test_activity_pages_of_30(env):
    _, cl = env
    p1 = cl.get("/api/activity", headers=H).json()
    assert len(p1["rows"]) == 30 and p1["nextBefore"]
    p2 = cl.get(f"/api/activity?before={p1['nextBefore']}", headers=H).json()
    p3 = cl.get(f"/api/activity?before={p2['nextBefore']}", headers=H).json()
    assert len(p2["rows"]) == 30 and len(p3["rows"]) == 5 and p3["nextBefore"] is None
    row = p1["rows"][0]
    assert row["link"] == coin_url(MINT, "solana")
    assert {r["open"] for r in p1["rows"]} == {"coin", "audit"}
    assert all(r["open"] == ("audit" if r["kind"] == "rejected" else "coin") for r in p1["rows"])


def test_coin(env):
    _, cl = env
    c = cl.get(f"/api/coin/{MINT}", headers=H).json()
    assert c["coin_url"] == coin_url(MINT, "solana") and c["button"] == "Buy on FOMO"
    assert c["chain"] == "Solana" and c["shield"] == "8/8" and c["price"] == 1.1
    k = c["candles"]
    assert sum(len([x]) for x in k) >= 1 and all({"t", "o", "h", "l", "c", "v"} <= set(x) for x in k)
    assert max(x["h"] for x in k) == 1.2 and min(x["l"] for x in k) == 0.9
    assert c["stats"]["holders"] == "n/a" and c["position"] is None


def test_audit(env):
    _, cl = env
    a = cl.get(f"/api/audit/{MINT}", headers=H).json()
    assert [l["value"] for l in a["layers"]][:4] == ["Revoked", "Revoked", "0.9% · sold 0", "16.8%"]
    assert a["headline"] == "Cleared · 8/8 parts" and a["enabled"] is True
    assert a["coin_url"] == coin_url(MINT, "solana")


def test_decisions(env):
    ctx, cl = env
    for i in range(25):
        ctx.conn.execute("INSERT INTO scans(mint, ts, verdict, reason) VALUES (?,?,?,?)",
                         (MINT, iso(ctx.now()), "kill", f"liq ${i}"))
    ctx.conn.commit()
    d = cl.get("/api/agents/scanner/decisions", headers=H).json()["decisions"]
    assert len(d) == 20 and d[0]["link"] == coin_url(MINT, "solana") and d[0]["coin"] == "Frog"
    for a in ("auditor", "analyst", "executor", "risk"):
        assert cl.get(f"/api/agents/{a}/decisions", headers=H).status_code == 200
    assert cl.get("/api/agents/bob/decisions", headers=H).status_code == 404


def test_account(env):
    _, cl = env
    a = cl.get("/api/account", headers=H).json()
    assert a["profile_url"] == profile_url("Cashoutkacper") and a["signup_url"] == SIGNUP_URL
    assert a["mode"] == "PAPER" and a["stack"] == 200


def test_settings_get_and_post(env):
    ctx, cl = env
    g = cl.get("/api/settings", headers=H).json()["groups"]
    assert {"scanner", "auditor", "analyst", "analyst_rules", "executor", "risk", "desk"} <= set(g)
    r = cl.post("/api/settings", headers=H, json={"key": "stop", "value": "-8%"})
    assert r.status_code == 200 and r.json()["message"] == "stop -6% -> -8%"
    assert ctx.conn.execute("SELECT key, new, who FROM change_log ORDER BY rowid DESC").fetchone()[:] == \
        ("risk.stop_pct", "8.0", "app")
    bad = cl.post("/api/settings", headers=H, json={"key": "stop", "value": 95})
    assert bad.status_code == 400 and "between 0.5 and 90" in bad.json()["error"]


def test_pause_resume_and_risk_lock(env):
    ctx, cl = env
    assert cl.post("/api/agents/scanner/pause", headers=H).json()["status"] == "paused"
    assert "scanner" in bus.paused
    assert cl.post("/api/agents/scanner/resume", headers=H).json()["status"] == "running"
    ctx.conn.execute("INSERT INTO positions(mint, opened_at, entry_price, size_usd, peak_price, stop_price,"
                     " trail_active, mode) VALUES (?,?,1,47.5,1,0.94,0,'PAPER')", (MINT, iso(ctx.now())))
    ctx.conn.commit()
    assert cl.post("/api/agents/risk/pause", headers=H).status_code == 409


def test_mode_live_rules(env):
    ctx, cl = env
    assert cl.post("/api/mode", headers=H, json={"mode": "LIVE"}).status_code == 400
    r = cl.post("/api/mode", headers=H, json={"mode": "LIVE", "confirm": "LIVE"})
    assert r.status_code == 400 and "full PAPER night" in r.json()["error"]
    # write a full previous night of balance rows (every 5 min)
    cfg = config.load(ctx.conn)
    from runtime import parse_iso
    end = parse_iso(config.night_start(cfg))
    t = end - timedelta(days=1)
    while t <= end:
        ctx.conn.execute("INSERT INTO balance(ts, value) VALUES (?, 200)", (iso(t),))
        t += timedelta(minutes=5)
    ctx.conn.commit()
    r = cl.post("/api/mode", headers=H, json={"mode": "LIVE", "confirm": "LIVE"})
    assert r.status_code == 200 and r.json()["new"] == "LIVE"
    assert cl.post("/api/mode", headers=H, json={"mode": "PAPER"}).json()["new"] == "PAPER"


def test_push_subscription_saved(env):
    ctx, cl = env
    body = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "k", "auth": "a"}}
    assert cl.post("/api/push", headers=H, json=body).json() == {"saved": True}
    assert ctx.conn.execute("SELECT endpoint FROM push_subs").fetchone()[0] == "https://push.example/abc"
    assert cl.post("/api/push", headers=H, json={"endpoint": "x"}).status_code == 400


def test_no_response_carries_the_token(env):
    _, cl = env
    for p in GETS[:-1]:
        assert TOKEN not in cl.get(p, headers=H).text


def test_stream_sse_with_token_query():
    """EventSource can't send headers: ?t= works for /api/stream only."""
    import asyncio
    import json as _j
    from starlette.requests import Request

    app = create_app(conn_getter=lambda: None, token_getter=lambda: TOKEN, serve_web=False)
    cl = TestClient(app)
    assert cl.get("/api/state?t=" + TOKEN).status_code == 401           # only the stream takes ?t=
    route = next(r for r in app.routes if getattr(r, "path", "") == "/api/stream")

    async def go():
        bus.init()
        scope = {"type": "http", "method": "GET", "path": "/api/stream", "headers": [],
                 "query_string": f"t={TOKEN}".encode()}

        async def receive():
            await asyncio.sleep(3600)
        resp = await route.endpoint(Request(scope, receive))
        it = resp.body_iterator
        first = await it.__anext__()
        bus.publish("fill", {"mint": MINT, "sizeUsd": 47.5})
        bus.publish("ignored_event", {})
        second = await it.__anext__()
        await it.aclose()
        return first, second
    first, second = asyncio.run(go())
    assert first.startswith(": connected")
    assert second.startswith("event: fill\ndata: ") and _j.loads(second.split("data: ")[1])["sizeUsd"] == 47.5
