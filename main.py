"""Start the desk in ONE asyncio event loop: the 5 agents, heartbeats, the API
(/health today; STEP 7 adds the rest). Telegram polling is STEP 9.

Mode PAPER by default. Nothing here or in the agents ever places an order.
"""

from __future__ import annotations

import asyncio
import os
import signal
from datetime import datetime, timezone

import httpx

import bus
import config
import db
import logutil
from agents import analyst, auditor, executor, risk, scanner
import notifier as notifier_mod
import telegram
from runtime import Ctx, iso
from sources import Sources

HEARTBEAT_S = 15
log = logutil.get("desk")


async def heartbeat_loop(ctx: Ctx, tasks: dict[str, asyncio.Task]) -> None:
    """Every ~15s: write db.heartbeats for each agent whose task is alive and beat recently."""
    while True:
        beats = bus.beats()
        now = datetime.now(timezone.utc)
        for name, t in tasks.items():
            b = beats.get(name)
            if t.done() or b is None or now.timestamp() - b > 60:
                continue
            ctx.conn.execute(
                "INSERT INTO heartbeats(agent, ts) VALUES (?, ?) ON CONFLICT(agent) DO UPDATE SET ts=excluded.ts",
                (name, iso(now)),
            )
        ctx.conn.execute(
            "INSERT INTO heartbeats(agent, ts) VALUES ('process', ?) ON CONFLICT(agent) DO UPDATE SET ts=excluded.ts",
            (iso(now),),
        )
        ctx.conn.commit()
        await asyncio.sleep(HEARTBEAT_S)


async def balance_loop(ctx: Ctx) -> None:
    """db.balance every minute (feeds the Home chart)."""
    while True:
        cfg = ctx.cfg()
        ctx.conn.execute("INSERT INTO balance(ts, value) VALUES (?, ?)",
                         (ctx.now_iso(), round(config.current_stack(ctx.conn, cfg), 2)))
        ctx.conn.commit()
        await asyncio.sleep(60)


async def start_of_night(ctx: Ctx) -> None:
    """STEP 10 ticket check at the start of every night (bump: tell, skip: warn).
    Never starts an empty night silently."""
    cfg = ctx.cfg()
    chk = config.ticket_check(cfg, config.current_stack(ctx.conn, cfg))
    log.info(f"ticket_check\t{chk['level']}\tmin_stack {chk['min_stack']}")
    if chk["message"]:
        await ctx.notifier.message(chk["message"], kind="ticket_check")


def _night_file():
    return config.LOG_DIR / "ticket_check_night.txt"


async def check_night_once(ctx: Ctx, quiet: bool = False) -> str:
    """Ticket check once per night: a restart inside the same night doesn't repeat it."""
    night = ctx.night_start()
    f = _night_file()
    done = f.read_text().strip() if f.exists() else ""
    if done != night:
        await start_of_night(ctx)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(night)
    elif not quiet:
        log.info("ticket_check\talready done this night")
    return night


async def night_loop(ctx: Ctx, first_night: str, every_s: float = 30) -> None:
    """Runs start_of_night once each time a new night starts (settings.report_time)
    while the process keeps running. The night the process started in was checked at boot."""
    last = first_night
    while True:
        await asyncio.sleep(every_s)
        try:
            n = ctx.night_start()
            if n != last:
                last = await check_night_once(ctx)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.info(f"night_loop_error\t{type(e).__name__}: {e}")


async def main() -> None:
    import uvicorn
    import api
    import publish

    logutil.quiet_http_libs()
    logutil.get("links")  # unknown-chain warnings from links.py -> logs/links.log
    db.init_db()
    conn = db.connect()
    config.seed(conn)
    cfg = config.load(conn)
    if not conn.execute("SELECT 1 FROM balance LIMIT 1").fetchone():
        conn.execute("INSERT INTO balance(ts, value) VALUES (?, ?)", (db.utc_now(), cfg["paper_start_stack"]))
        conn.commit()
    bus.init()

    client = httpx.AsyncClient(headers={"User-Agent": "desk/1.0"},
                               limits=httpx.Limits(max_connections=20))
    src = Sources(client, lambda: config.load(conn)["rpc_url"], analyst.jev_key,
                  max_rpm=cfg["scanner.max_dex_rpm"])
    notif = notifier_mod.Notifier() if os.environ.get("DESK_NOTIFIER") == "outbox" else notifier_mod.build(cfg, client)
    ctx = Ctx(conn=conn, src=src, notifier=notif)
    log.info(f"start\tmode {cfg['mode']}\tanalyst {cfg['analyst_mode']}\tnotifier {notif.channel}")
    first_night = await check_night_once(ctx)
    ctx.night_hook = lambda: check_night_once(ctx, quiet=True)

    host = os.environ.get("DESK_HOST", "127.0.0.1")
    port = int(os.environ.get("DESK_PORT", "8000"))
    tg = isinstance(notif, notifier_mod.TelegramNotifier)
    app = api.create_app(conn_getter=lambda: conn, telegram_connected=lambda: tg)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning",
                                          access_log=False))  # no URLs (SSE ?t=) in logs

    agent_tasks = {
        "scanner": asyncio.create_task(scanner.run(ctx), name="scanner"),
        "auditor": asyncio.create_task(auditor.run(ctx), name="auditor"),
        "analyst": asyncio.create_task(analyst.run(ctx), name="analyst"),
        "executor": asyncio.create_task(executor.run(ctx), name="executor"),
        "risk": asyncio.create_task(risk.run(ctx), name="risk"),
    }
    others = [
        asyncio.create_task(heartbeat_loop(ctx, agent_tasks), name="heartbeats"),
        asyncio.create_task(balance_loop(ctx), name="balance"),
        asyncio.create_task(night_loop(ctx, first_night), name="night"),
    ]
    if config.read_secret("DATA_PATH"):
        # plan B: same shapes as files under site/d/<data path>/; pushed to gh-pages only
        # when a push credential exists (secrets/GH_PAGES_TOKEN), throttled for Pages.
        deployer = publish.GhPagesDeployer(publish.SITE_DIR, publish.PAGES_REMOTE)
        others.append(asyncio.create_task(
            publish.run(conn, telegram_connected=tg, dex=src.dex,
                        deployer=deployer if deployer.enabled() else None), name="publish"))
    if tg:
        # STEP 9: long polling, owner only; commands + [Bought]/[Skip]/[Sold] callbacks
        others.append(asyncio.create_task(
            telegram.run_polling(conn, notif, auditor=auditor.Auditor(ctx), dex=src.dex,
                                 on_change=publish.request_republish), name="telegram"))
    others.append(asyncio.create_task(server.serve(), name="api"))  # keep last (clean shutdown)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async def watch():
        # an agent that crashes is restarted (desk-health in STEP 12 covers hangs)
        while not stop.is_set():
            for name, t in list(agent_tasks.items()):
                if t.done() and not t.cancelled():
                    exc = t.exception()
                    log.info(f"agent_crashed\t{name}\t{type(exc).__name__}: {exc}")
                    mod = {"scanner": scanner, "auditor": auditor, "analyst": analyst,
                           "executor": executor, "risk": risk}[name]
                    agent_tasks[name] = asyncio.create_task(mod.run(ctx), name=name)
            await asyncio.sleep(5)

    watcher = asyncio.create_task(watch())
    await stop.wait()
    log.info("stopping")
    server.should_exit = True  # uvicorn shuts down on its own (no lifespan traceback)
    api_task = others.pop()
    for t in [*agent_tasks.values(), *others, watcher]:
        t.cancel()
    await asyncio.gather(*agent_tasks.values(), *others, watcher, return_exceptions=True)
    try:
        await asyncio.wait_for(api_task, timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        api_task.cancel()
    await client.aclose()
    conn.commit()
    conn.close()
    log.info("stopped")


if __name__ == "__main__":
    asyncio.run(main())
