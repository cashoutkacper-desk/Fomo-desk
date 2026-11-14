"""STEP 7 · API, plan A (JSON + SSE for the app). Plan B writes the same shapes
as files (publish.py); this desk runs plan B, so this server is NOT exposed
publicly (it binds 127.0.0.1) and is the same contract for local use/tests.

Every /api call needs APP_TOKEN (header "Authorization: Bearer <t>" or
"X-App-Token: <t>"; SSE may pass ?t= because EventSource can't set headers),
else 401. The token is compared in constant time, never logged, never returned.
/health stays open. No response ever carries a secret.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import sqlite3
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import bus
import config
import shapes

WEB_DIR = Path(__file__).resolve().parent / "web"
SSE_EVENTS = ("scan", "audit_layer", "verdict", "signal", "fill", "exit", "balance")


def create_app(conn_getter: Callable[[], sqlite3.Connection] | None = None,
               token_getter: Callable[[], str | None] | None = None,
               telegram_connected: Callable[[], bool] | None = None,
               serve_web: bool = True) -> FastAPI:
    app = FastAPI(title="desk", docs_url=None, redoc_url=None, openapi_url=None)
    _conn: dict[str, sqlite3.Connection] = {}

    def conn() -> sqlite3.Connection:
        if conn_getter:
            return conn_getter()
        if "c" not in _conn:
            import db
            _conn["c"] = db.connect()
        return _conn["c"]

    def token() -> str | None:
        return (token_getter or (lambda: config.read_secret("APP_TOKEN")))()

    def auth(request: Request) -> None:
        expected = token()
        given = request.headers.get("x-app-token") or ""
        h = request.headers.get("authorization") or ""
        if h.lower().startswith("bearer "):
            given = h[7:].strip()
        if not given and request.url.path == "/api/stream":
            given = request.query_params.get("t", "")
        if not expected or not given or not hmac.compare_digest(given.encode(), expected.encode()):
            raise HTTPException(status_code=401, detail="unauthorized")

    def bad(e: Exception):
        return JSONResponse({"error": str(e)}, status_code=400)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/state", dependencies=[Depends(auth)])
    async def state():
        return shapes.state(conn())

    @app.get("/api/chart", dependencies=[Depends(auth)])
    async def chart(range: str = "LIVE"):  # noqa: A002
        try:
            return shapes.chart(conn(), range)
        except ValueError as e:
            return bad(e)

    @app.get("/api/activity", dependencies=[Depends(auth)])
    async def activity(before: int | None = None):
        return shapes.activity(conn(), before)

    @app.get("/api/coin/{mint}", dependencies=[Depends(auth)])
    async def coin(mint: str):
        return shapes.coin(conn(), mint)

    @app.get("/api/audit/{mint}", dependencies=[Depends(auth)])
    async def audit(mint: str):
        return shapes.audit(conn(), config.load(conn()), mint)

    @app.get("/api/agents/{name}/decisions", dependencies=[Depends(auth)])
    async def decisions(name: str):
        try:
            return shapes.decisions(conn(), name)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=404)

    @app.get("/api/account", dependencies=[Depends(auth)])
    async def account():
        return shapes.account(conn(), (telegram_connected or (lambda: False))())

    @app.get("/api/settings", dependencies=[Depends(auth)])
    async def get_settings():
        return shapes.settings(conn())

    @app.post("/api/settings", dependencies=[Depends(auth)])
    async def post_settings(body: dict[str, Any]):
        """{"key": name or alias, "value": v} -> validated, written to change_log."""
        try:
            r = config.set_value(conn(), str(body.get("key", "")), body.get("value"), who="app")
        except config.SettingError as e:
            return bad(e)
        return {"key": r["key"], "old": r["old"], "new": r["new"], "message": r["message"],
                "ticket": r["ticket"]}

    @app.post("/api/agents/{name}/pause", dependencies=[Depends(auth)])
    async def pause(name: str):
        if name not in bus.AGENTS:
            return JSONResponse({"error": "unknown agent"}, status_code=404)
        open_n = conn().execute("SELECT COUNT(*) FROM positions WHERE closed_at IS NULL").fetchone()[0]
        try:
            bus.pause(name, open_n)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        return {"agent": name, "status": "paused"}

    @app.post("/api/agents/{name}/resume", dependencies=[Depends(auth)])
    async def resume(name: str):
        if name not in bus.AGENTS:
            return JSONResponse({"error": "unknown agent"}, status_code=404)
        bus.resume(name)
        return {"agent": name, "status": "running"}

    @app.post("/api/mode", dependencies=[Depends(auth)])
    async def mode(body: dict[str, Any]):
        try:
            return config.set_mode(conn(), str(body.get("mode", "")), body.get("confirm"), who="app")
        except config.SettingError as e:
            return bad(e)

    @app.post("/api/push", dependencies=[Depends(auth)])
    async def push(body: dict[str, Any]):
        """Save a web push subscription (STEP 8 uses it; plan A only)."""
        ep = body.get("endpoint")
        keys = body.get("keys") or {}
        if not ep or not str(ep).startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
            return bad(ValueError("subscription needs endpoint, keys.p256dh, keys.auth"))
        from db import utc_now
        c = conn()
        c.execute("INSERT OR REPLACE INTO push_subs(endpoint, p256dh, auth, created_at) VALUES (?,?,?,?)",
                  (ep, keys["p256dh"], keys["auth"], utc_now()))
        c.commit()
        return {"saved": True}

    @app.get("/api/stream", dependencies=[Depends(auth)])
    async def stream(request: Request):
        q = bus.subscribe()

        async def gen():
            try:
                yield ": connected\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event, data = await asyncio.wait_for(q.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    if event in SSE_EVENTS:
                        yield f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
            finally:
                bus.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store"})

    # Static web/ for local plan A serving. It must never contain secrets.
    if serve_web and WEB_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
    return app


app = create_app()
