"""STEP 7 · plan B: the same shapes as api.py, written as JSON files.

Layout of the published site (the gh-pages branch content):
    index.html, app.js, ... (copied from web/)          <- the app, no data
    .nojekyll
    d/{DATA_PATH}/state.json                            GET /api/state
    d/{DATA_PATH}/chart.json                            GET /api/chart (all 6 ranges, keyed)
    d/{DATA_PATH}/activity.json                         GET /api/activity (newest 30)
    d/{DATA_PATH}/activity/{before}.json                GET /api/activity?before= (older pages)
    d/{DATA_PATH}/coin/{mint}.json                      GET /api/coin/{mint}
    d/{DATA_PATH}/audit/{mint}.json                     GET /api/audit/{mint}
    d/{DATA_PATH}/agents/{name}.json                    GET /api/agents/{name}/decisions
    d/{DATA_PATH}/account.json                          GET /api/account
    d/{DATA_PATH}/settings.json                         GET /api/settings (read-only in plan B)
No index.html anywhere under d/, so the bare /d/ and /d/{DATA_PATH}/ are 404 on
static hosting (GitHub Pages has no directory listing). POST endpoints and SSE
don't exist in plan B: settings, pause and mode go through Telegram (STEP 9).

DATA_PATH is read from secrets/ and is only ever used as that directory name.
It is never logged, never written into a file, never in the main branch.
Every file is checked against every secret value before it is written.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
import logutil
import shapes
from db import insert_price

SITE_DIR = config.DESK_ROOT / "site"           # gitignored; the gh-pages working tree
WEB_DIR = config.DESK_ROOT / "web"
SECRET_NAMES = ("TYPESAFE_API_KEY", "TG_TOKEN", "APP_TOKEN", "DATA_PATH", "GH_PAGES_TOKEN")
WRITE_EVERY_S = 15
PAGES_REMOTE = "https://github.com/cashoutkacper-desk/Fomo-desk.git"
PRICES_EVERY_S = 60
log = logutil.get("publish")


class SecretLeak(RuntimeError):
    """A file about to be written contains a secret value. It is not written."""


def secret_values() -> list[str]:
    vals = [v for v in (config.read_secret(n) for n in SECRET_NAMES) if v]
    pem = config.SECRETS_DIR / "vapid_private.pem"
    if pem.is_file():
        body = "".join(l for l in pem.read_text().splitlines() if "-----" not in l).strip()
        if body:
            vals.append(body)
    return vals


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


class Publisher:
    def __init__(self, conn, site_dir: Path = SITE_DIR, data_path: str | None = None,
                 secrets: list[str] | None = None, telegram_connected: bool = False, dex=None):
        self.conn = conn
        self.site = Path(site_dir)
        self.data_path = data_path if data_path is not None else config.read_secret("DATA_PATH")
        if not self.data_path or len(self.data_path) < 32 or "/" in self.data_path:
            raise ValueError("DATA_PATH missing or shorter than 32 characters")
        self.secrets = secrets if secrets is not None else secret_values()
        self.telegram_connected = telegram_connected
        self.dex = dex                       # sources.Dex for the api prices writer
        self.dex_cache: dict[str, dict] = {}
        self.writes = 0

    @property
    def data_dir(self) -> Path:
        return self.site / "d" / self.data_path

    # ---------- files
    def _write(self, rel: str, obj: Any) -> None:
        text = _dump(obj)
        for v in self.secrets:
            if v and v in text:
                raise SecretLeak(f"{rel}: refused, would contain a secret")
        path = self.data_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)

    def copy_web(self) -> None:
        """The app itself (web/) at the site root. web/ never holds data or secrets."""
        self.site.mkdir(parents=True, exist_ok=True)
        for p in WEB_DIR.rglob("*"):
            if p.is_file():
                dst = self.site / p.relative_to(WEB_DIR)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dst)
        (self.site / ".nojekyll").write_text("")
        for idx in (self.site / "d" / "index.html", self.data_dir / "index.html"):
            if idx.exists():
                idx.unlink()

    def coins_tonight(self, now) -> list[str]:
        cfg = config.load(self.conn)
        ns = config.night_start(cfg, now)
        rows = self.conn.execute(
            "SELECT mint FROM activity WHERE ts >= ? AND mint IS NOT NULL "
            "UNION SELECT mint FROM signals WHERE ts >= ? "
            "UNION SELECT mint FROM audits WHERE ts >= ? "
            "UNION SELECT mint FROM positions WHERE closed_at IS NULL OR closed_at >= ? "
            "UNION SELECT mint FROM scans WHERE ts >= ?", (ns, ns, ns, ns, ns)).fetchall()
        return sorted({r[0] for r in rows})

    def write_all(self, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        c = self.conn
        n = 0
        self._write("state.json", shapes.state(c, now)); n += 1
        self._write("chart.json", shapes.chart_all(c, now)); n += 1
        page = shapes.activity(c)
        self._write("activity.json", page); n += 1
        for _ in range(10):                      # older pages, as activity/{before}.json
            if not page["nextBefore"]:
                break
            before = page["nextBefore"]
            page = shapes.activity(c, before)
            self._write(f"activity/{before}.json", page); n += 1
        cfg = config.load(c)
        for m in self.coins_tonight(now):
            self._write(f"coin/{m}.json", shapes.coin(c, m, now, self.dex_cache.get(m))); n += 1
            self._write(f"audit/{m}.json", shapes.audit(c, cfg, m)); n += 1
        for a in shapes.AGENTS:
            self._write(f"agents/{a}.json", shapes.decisions(c, a)); n += 1
        self._write("account.json", shapes.account(c, self.telegram_connected)); n += 1
        self._write("settings.json", shapes.settings(c) | {"readOnly": True}); n += 1
        self.writes += 1
        return n

    # ---------- api.py's db.prices job (STEP 4): every 60s, coins pushed or audited
    # tonight that neither Scanner (waiting room) nor Risk (open positions) covers.
    async def prices_once(self, now=None) -> int:
        if self.dex is None:
            return 0
        now = now or datetime.now(timezone.utc)
        c = self.conn
        cfg = config.load(c)
        ns = config.night_start(cfg, now)
        rows = c.execute(
            "SELECT mint FROM signals WHERE ts >= ? UNION SELECT mint FROM audits WHERE ts >= ?", (ns, ns)
        ).fetchall()
        covered = {r[0] for r in c.execute("SELECT mint FROM positions WHERE closed_at IS NULL")}
        covered |= {r[0] for r in c.execute(
            "SELECT mint FROM coins co WHERE migrated_at >= ? AND NOT EXISTS "
            "(SELECT 1 FROM scans s WHERE s.mint=co.mint)", (ns,))}
        mints = sorted({r[0] for r in rows} - covered)
        if not mints:
            return 0
        views = await self.dex.tokens(mints)
        ts = shapes.iso(now)
        for m, v in views.items():
            self.dex_cache[m] = v
            insert_price(c, "api", m, ts, v.get("priceUsd"), v.get("volumeM5"))
        c.commit()
        return len(views)


class GhPagesDeployer:
    """Pushes the site dir as ONE orphan commit to the gh-pages branch (force).
    Only gh-pages is touched; main (code) never sees the site or DATA_PATH.

    Auth: GIT_ASKPASS reads secrets/GH_PAGES_TOKEN at push time; the token is never
    in the remote URL, .git/config, argv or a log line. No token -> disabled.
    GitHub Pages rebuilds at most ~10 times/hour (soft limit) and serves with
    max-age=600, so pushes are throttled to deploy_interval_s.
    """

    def __init__(self, site_dir: Path, remote: str, branch: str = "gh-pages",
                 token_name: str | None = "GH_PAGES_TOKEN", min_interval_s: int = 360):
        self.site = Path(site_dir)
        self.remote = remote
        self.branch = branch
        self.token_name = token_name
        self.min_interval_s = min_interval_s
        self.last_push: float | None = None
        self.last_sha: str | None = None
        if branch in ("main", "master"):
            raise ValueError("the deployer only pushes gh-pages")

    def enabled(self) -> bool:
        return self.token_name is None or bool(config.read_secret(self.token_name))

    def _git(self, *args: str, env: dict | None = None) -> str:
        r = subprocess.run(["git", *args], cwd=self.site, capture_output=True, text=True,
                           env={**os.environ, **(env or {})}, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(f"git {args[0]} failed ({r.returncode})")  # no stderr: could echo URLs
        return r.stdout.strip()

    def deploy(self, message: str = "desk data", force: bool = False) -> str | None:
        import time

        if not self.enabled():
            log.info("deploy skipped: no push credential")
            return None
        if not force and self.last_push and time.time() - self.last_push < self.min_interval_s:
            return None
        env = {"GIT_TERMINAL_PROMPT": "0"}
        if self.token_name:
            askpass = config.DESK_ROOT / "tools" / "git_askpass.sh"
            env.update({"GIT_ASKPASS": str(askpass), "DESK_TOKEN_FILE": str(config.SECRETS_DIR / self.token_name)})
        if not (self.site / ".git").exists():
            self._git("init", "-q")
        self._git("config", "user.name", "desk")
        self._git("config", "user.email", "desk@users.noreply.github.com")
        self._git("checkout", "-q", "--orphan", "_publish")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", message)
        self._git("branch", "-M", self.branch)
        self._git("push", "-q", "-f", self.remote, f"{self.branch}:{self.branch}", env=env)
        self.last_sha = self._git("rev-parse", "HEAD")
        self.last_push = time.time()
        log.info(f"deployed {self.branch} {self.last_sha[:12]}")
        return self.last_sha


_kick: asyncio.Event | None = None


def request_republish() -> None:
    """STEP 9: after a Telegram change (setting, pause, mode) write the files now and
    deploy as soon as the Pages throttle allows, instead of waiting for the next tick."""
    if _kick is not None:
        _kick.set()


async def run(conn, telegram_connected: bool = False, dex=None, deployer: GhPagesDeployer | None = None) -> None:
    """Plan B loop: files every 15s, prices every 60s, deploy when allowed."""
    global _kick
    _kick = asyncio.Event()
    pub = Publisher(conn, telegram_connected=telegram_connected, dex=dex)
    pub.copy_web()
    last_prices = 0.0
    loop = asyncio.get_running_loop()
    while True:
        try:
            if loop.time() - last_prices >= PRICES_EVERY_S:
                last_prices = loop.time()
                try:
                    await asyncio.wait_for(pub.prices_once(), 60)   # wall-clock cap
                except asyncio.TimeoutError:
                    log.info("prices_timeout\t60s")
            pub.write_all()
            if deployer is not None:
                # git runs with subprocess timeout=120 (killed on expiry); this caps the wait too
                await asyncio.wait_for(asyncio.to_thread(deployer.deploy), 150)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.info(f"publish_error\t{type(e).__name__}: {e}")
        try:
            await asyncio.wait_for(_kick.wait(), timeout=WRITE_EVERY_S)
        except asyncio.TimeoutError:
            pass
        _kick.clear()
