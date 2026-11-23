"""Settings (STEP 10): defaults, sane min/max, validation, change log, ticket check.

Every number in [brackets] in the agent prompts is a setting here. Keys of the
five agents are namespaced ("scanner.poll_s", "risk.stop_pct") because STEP 10
reuses names across agents (poll_s, min_pool_age_min, max_top10_pct,
confirm_window_min). Desk keys and analyst_mode stay flat, matching the keys
already in db.settings from STEPS 1-4 (fomo_handle, analyst_mode, owner_chat_id).

Values are read live: agents call load(conn) every loop, so a change applies on
the next poll. Secrets never live here.
"""

from __future__ import annotations

import math
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

DESK_ROOT = Path(__file__).resolve().parent
DB_PATH = DESK_ROOT / "desk.db"
SECRETS_DIR = DESK_ROOT / "secrets"
LOG_DIR = Path(os.environ.get("DESK_LOG_DIR") or (DESK_ROOT / "logs"))  # tests point this at a temp dir
LOCAL_TZ = ZoneInfo("America/New_York")


class SettingError(ValueError):
    """Refused setting change. The message says the allowed range."""


@dataclass(frozen=True)
class Spec:
    key: str
    group: str            # scanner | auditor | analyst | analyst_rules | executor | risk | desk
    default: Any
    kind: str             # float | int | bool | str | choice | time
    lo: float | None = None
    hi: float | None = None
    choices: tuple[str, ...] = ()
    step10: bool = True   # listed in STEP 10 (False = other [bracket] number from the agent prompts)
    label: str = ""


def _s(*a, **k) -> Spec:
    return Spec(*a, **k)


SPECS: tuple[Spec, ...] = (
    # ---- scanner
    _s("scanner.min_liquidity_usd", "scanner", 40000.0, "float", 1000, 10_000_000),
    _s("scanner.max_top10_pct", "scanner", 25.0, "float", 1, 100),
    _s("scanner.max_h1_change_pct", "scanner", 80.0, "float", 1, 10_000),
    _s("scanner.poll_s", "scanner", 60, "int", 15, 600),
    _s("scanner.min_pool_age_min", "scanner", 10, "int", 0, 240),
    _s("scanner.max_wait_min", "scanner", 60, "int", 5, 720),
    _s("scanner.runner_h24_pct", "scanner", 300.0, "float", 10, 100_000),
    _s("scanner.max_dex_rpm", "scanner", 250, "int", 10, 290, step10=False),
    _s("scanner.ws_silence_s", "scanner", 120, "int", 30, 900, step10=False),
    _s("scanner.dex_429_wait_s", "scanner", 30, "int", 5, 300, step10=False),
    # Kacper, Nov 20: after N connection refusals in a row wait M min, then the normal backoff
    _s("scanner.refused_cooldown_after", "scanner", 10, "int", 2, 100, step10=False),
    _s("scanner.refused_cooldown_min", "scanner", 15, "int", 1, 120, step10=False),
    # on-chain backup feed (Kacper Nov 23): poll pump.fun's migration authority over our RPC
    _s("scanner.onchain_feed", "scanner", "on", "choice", choices=("on", "off"), step10=False),
    _s("scanner.onchain_poll_s", "scanner", 15, "int", 5, 300, step10=False),
    _s("scanner.onchain_safety_poll_s", "scanner", 60, "int", 15, 900, step10=False),
    # ---- auditor
    _s("auditor.enabled", "auditor", True, "bool"),
    _s("auditor.max_dev_pct", "auditor", 5.0, "float", 0, 100),
    _s("auditor.max_top10_pct", "auditor", 25.0, "float", 1, 100),
    _s("auditor.min_lp_locked_pct", "auditor", 99.0, "float", 50, 100),
    _s("auditor.min_pool_age_min", "auditor", 10, "int", 0, 240),
    _s("auditor.max_bundled", "auditor", 5, "int", 1, 1000),
    _s("auditor.max_delegated_accounts", "auditor", 10, "int", 1, 10_000),
    _s("auditor.retries", "auditor", 3, "int", 0, 10),
    _s("auditor.retry_delay_s", "auditor", 2.0, "float", 0, 30, step10=False),
    _s("auditor.max_liq_drop_pct", "auditor", 50.0, "float", 1, 100, step10=False),
    # ---- analyst
    _s("analyst_mode", "analyst", "jev", "choice", choices=("jev", "rules")),
    _s("analyst.min_alive_6h", "analyst", 0.70, "float", 0, 1),
    _s("analyst.min_organic", "analyst", 0.65, "float", 0, 1),
    _s("analyst.max_chase", "analyst", 0.30, "float", 0, 1),
    _s("analyst.min_score", "analyst", 70, "int", 0, 100),
    _s("analyst.timeout_s", "analyst", 10.0, "float", 2, 60),
    _s("analyst.max_state_chars", "analyst", 3000, "int", 500, 20_000, step10=False),
    _s("analyst.retry_delay_s", "analyst", 2.0, "float", 0, 30, step10=False),
    _s("analyst.queue_429_s", "analyst", 60, "int", 0, 600, step10=False),
    # ---- analyst (rules)
    _s("analyst.rules.alive_volume_h1", "analyst_rules", 25000.0, "float", 100, 100_000_000),
    _s("analyst.rules.organic_buys_h1", "analyst_rules", 150, "int", 1, 1_000_000),
    _s("analyst.rules.bundled_penalty", "analyst_rules", 0.5, "float", 0, 1),
    _s("analyst.rules.bundled_min_wallets", "analyst_rules", 3, "int", 1, 1000),  # "(from 3 wallets)"
    _s("analyst.rules.flush_drop_m5_pct", "analyst_rules", 5.0, "float", 0.1, 100),
    _s("analyst.rules.chase_h1_pct", "analyst_rules", 80.0, "float", 1, 10_000),
    # ---- executor
    _s("executor.stack_pct", "executor", 8.0, "float", 0.5, 100),
    _s("executor.max_pool_pct", "executor", 2.0, "float", 0.1, 50),
    _s("executor.min_ticket_usd", "executor", 47.50, "float", 5, 100_000),
    _s("executor.below_min", "executor", "bump", "choice", choices=("bump", "skip")),
    _s("executor.recheck_liq_drop_pct", "executor", 20.0, "float", 1, 100),
    _s("executor.recheck_price_up_pct", "executor", 15.0, "float", 1, 1000),
    _s("executor.confirm_window_min", "executor", 5, "int", 1, 120),
    # ---- risk
    _s("risk.poll_s", "risk", 15, "int", 5, 300),
    _s("risk.stop_pct", "risk", 6.0, "float", 0.5, 90),
    _s("risk.trail_start_pct", "risk", 20.0, "float", 1, 10_000),
    _s("risk.trail_pct", "risk", 12.0, "float", 0.5, 90),
    _s("risk.timeout_h", "risk", 3.0, "float", 0.25, 72),
    _s("risk.timeout_min_gain_pct", "risk", 5.0, "float", -50, 1000),
    _s("risk.confirm_window_min", "risk", 2, "int", 1, 60),
    _s("risk.no_price_polls", "risk", 4, "int", 1, 100, step10=False),
    _s("risk.backoff_429_s", "risk", 30, "int", 5, 300, step10=False),
    # ---- desk
    _s("mode", "desk", "PAPER", "choice", choices=("PAPER", "LIVE")),
    _s("paper_start_stack", "desk", 200.0, "float", 10, 10_000_000),
    _s("report_time", "desk", "08:00", "time"),
    _s("notifier", "desk", "telegram", "choice", choices=("telegram", "webpush", "channel")),
    _s("rpc_url", "desk", "https://api.mainnet-beta.solana.com", "str"),
    _s("push_rejects", "desk", False, "bool"),
    # FOMO gives 10% off fees only to accounts that signed up through
    # links.SIGNUP_URL (STEP 1). Kacper's account did NOT sign up
    # through that link (bootstrap-settings.json: signup_referral_savipww=false),
    # so the discount does not apply to him. Spec default stays 0.10 here; the
    # effective default for this desk is 0 (desk_default(), from
    # bootstrap-settings.json), and desk.db holds 0 (set 2026-11-13, in change_log).
    _s("fees_ref_discount", "desk", 0.10, "float", 0, 0.5),
    _s("fomo_handle", "desk", "", "str"),
    # not in STEP 10, needed elsewhere in the spec
    _s("live_stack", "desk", 0.0, "float", 0, 100_000_000, step10=False),   # "stack 1240" (STEP 9)
)

SPEC_BY_KEY = {s.key: s for s in SPECS}

# Plain-language aliases ("set stop to -8%"). STEP 9 parses commands with these.
ALIASES = {
    "stop": "risk.stop_pct",
    "trail": "risk.trail_pct",
    "trail_start": "risk.trail_start_pct",
    "timeout": "risk.timeout_h",
    "stack_pct": "executor.stack_pct",
    "ticket": "executor.min_ticket_usd",
    "min_ticket": "executor.min_ticket_usd",
    "below_min": "executor.below_min",
    "min_liquidity": "scanner.min_liquidity_usd",
    "liquidity": "scanner.min_liquidity_usd",
    "min_score": "analyst.min_score",
    "score": "analyst.min_score",
    "stack": "live_stack",
    "analyst_mode": "analyst_mode",
}


def resolve_key(name: str) -> str:
    n = name.strip().lower().replace(" ", "_")
    if n in SPEC_BY_KEY:
        return n
    if n in ALIASES:
        return ALIASES[n]
    hits = [k for k in SPEC_BY_KEY if k.split(".")[-1] == n]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise SettingError(f"'{name}' is ambiguous: {', '.join(hits)}")
    raise SettingError(f"unknown setting '{name}'")


def _fmt(x: float) -> str:
    return f"{x:g}" if isinstance(x, float) else str(x)


def coerce(key: str, value: Any) -> Any:
    """Parse + validate one value. Out of range -> SettingError naming the range."""
    spec = SPEC_BY_KEY.get(key)
    if spec is None:
        raise SettingError(f"unknown setting '{key}'")
    v = value
    if spec.kind == "bool":
        if isinstance(v, bool):
            return v
        t = str(v).strip().lower()
        if t in ("1", "true", "on", "yes"):
            return True
        if t in ("0", "false", "off", "no", ""):
            return False
        raise SettingError(f"{key} must be true or false")
    if spec.kind in ("float", "int"):
        try:
            if isinstance(v, str):
                v = v.strip().replace("$", "").replace("%", "").replace(",", "")
            num = float(v)
        except (TypeError, ValueError):
            raise SettingError(f"{key} must be a number between {_fmt(spec.lo)} and {_fmt(spec.hi)}")
        if key == "risk.stop_pct" and num < 0:
            num = -num  # "set stop to -8%" means an 8% stop
        if math.isnan(num) or num < spec.lo or num > spec.hi:
            raise SettingError(
                f"{key} must be between {_fmt(spec.lo)} and {_fmt(spec.hi)} (got {_fmt(num)})"
            )
        if spec.kind == "int":
            if num != int(num):
                raise SettingError(f"{key} must be a whole number between {_fmt(spec.lo)} and {_fmt(spec.hi)}")
            return int(num)
        return num
    if spec.kind == "choice":
        t = str(v).strip()
        for c in spec.choices:
            if t.lower() == c.lower():
                return c
        raise SettingError(f"{key} must be one of {', '.join(spec.choices)}")
    if spec.kind == "time":
        t = str(v).strip()
        try:
            hh, mm = t.split(":")
            h, m = int(hh), int(mm)
            assert 0 <= h < 24 and 0 <= m < 60
        except Exception:
            raise SettingError(f"{key} must be HH:MM between 00:00 and 23:59")
        return f"{h:02d}:{m:02d}"
    if key == "rpc_url":
        t = str(v).strip()
        if not t.startswith("https://"):
            raise SettingError("rpc_url must start with https://")
        return t
    if key == "fomo_handle":
        return str(v).strip().lstrip("@")
    return str(v)


def defaults() -> dict[str, Any]:
    return {s.key: s.default for s in SPECS}


def referral_signup() -> bool | None:
    """bootstrap-settings.json 'signup_referral_savipww' (STEP 1 answer), or None if unknown."""
    import json

    p = DESK_ROOT / "bootstrap-settings.json"
    try:
        v = json.loads(p.read_text()).get("signup_referral_savipww")
    except (OSError, ValueError):
        return None
    return v if isinstance(v, bool) else None


def desk_default(key: str) -> Any:
    """Effective default for THIS desk. Same as the spec default, except
    fees_ref_discount = 0 when the owner did not sign up through the savipww
    referral (Kacper: signup_referral_savipww=false, decided 2026-11-13)."""
    if key == "fees_ref_discount" and referral_signup() is False:
        return 0.0
    return SPEC_BY_KEY[key].default


def seed(conn: sqlite3.Connection) -> int:
    """Insert every missing setting with its desk default. Existing values are kept
    (fomo_handle, analyst_mode, owner_chat_id from STEPS 1-2)."""
    from db import utc_now

    n = 0
    ts = utc_now()
    for spec in SPECS:
        if conn.execute("SELECT 1 FROM settings WHERE key=?", (spec.key,)).fetchone():
            continue
        if spec.key == "fomo_handle":
            continue  # only ever set from STEP 1
        val = desk_default(spec.key)
        text = ("true" if val else "false") if isinstance(val, bool) else str(val)
        conn.execute(
            "INSERT INTO settings(key, value, updated_at) VALUES (?,?,?)", (spec.key, text, ts)
        )
        n += 1
    conn.commit()
    return n


def load(conn: sqlite3.Connection) -> dict[str, Any]:
    """Typed settings: db value if valid, else the desk default."""
    out = {k: desk_default(k) for k in SPEC_BY_KEY}
    for row in conn.execute("SELECT key, value FROM settings"):
        k, v = row[0], row[1]
        if k in SPEC_BY_KEY:
            try:
                out[k] = coerce(k, v)
            except SettingError:
                pass
        else:
            out[k] = v  # owner_chat_id and other non-tunable keys
    return out


TICKET_KEYS = ("live_stack", "executor.stack_pct", "executor.below_min", "executor.min_ticket_usd",
               "paper_start_stack", "mode")


def set_value(conn: sqlite3.Connection, name: str, value: Any, who: str = "owner") -> dict[str, Any]:
    """Validate, write to db.settings + change_log. Returns {"key","old","new","message","ticket"}.

    Raises SettingError (with the allowed range) when refused.
    """
    from db import set_setting

    key = resolve_key(name)
    if key == "mode":
        raise SettingError("mode changes go through the PAPER/LIVE switch (STEP 11)")
    old = load(conn)[key]
    new = coerce(key, value)
    text = ("true" if new else "false") if isinstance(new, bool) else str(new)
    set_setting(conn, key, text, who)
    conn.commit()
    short = key.split(".")[-1]
    if key == "risk.stop_pct":
        msg = f"stop -{_fmt(old)}% -> -{_fmt(new)}%"
    else:
        msg = f"{short} {_fmt(old)} -> {_fmt(new)}"
    ticket = None
    if key in TICKET_KEYS:
        cfg = load(conn)
        ticket = ticket_check(cfg, current_stack(conn, cfg))
    return {"key": key, "old": old, "new": new, "message": msg, "ticket": ticket}


# ---------------------------------------------------------------- ticket check

def min_stack(cfg: dict[str, Any]) -> float:
    return cfg["executor.min_ticket_usd"] / (cfg["executor.stack_pct"] / 100.0)


def ticket_check(cfg: dict[str, Any], stack: float) -> dict[str, Any]:
    """STEP 10 ticket check. Run on every stack / stack_pct / below_min change and
    at the start of every night. Returns {"ok", "level", "message"}."""
    ms = round(min_stack(cfg), 2)
    ticket = cfg["executor.min_ticket_usd"]
    if stack >= ms:
        return {"ok": True, "level": "ok", "min_stack": ms, "message": None}
    if cfg["executor.below_min"] == "bump":
        p = ticket / stack * 100 if stack > 0 else float("inf")
        return {
            "ok": True,
            "level": "bump",
            "min_stack": ms,
            "message": f"tickets bumped to ${ticket:.2f} = {p:.1f}% of stack",
        }
    return {
        "ok": False,
        "level": "skip",
        "min_stack": ms,
        "message": (
            f"every coin will be skipped, stack under ${ms:,.2f}. "
            "Switch below_min to bump or raise the stack."
        ),
    }


def current_stack(conn: sqlite3.Connection, cfg: dict[str, Any]) -> float:
    """PAPER: the simulated balance. LIVE: the stack set with 'stack 1240'."""
    if cfg["mode"] == "LIVE":
        return float(cfg["live_stack"])
    row = conn.execute("SELECT value FROM balance ORDER BY rowid DESC LIMIT 1").fetchone()
    return float(row[0]) if row and row[0] is not None else float(cfg["paper_start_stack"])


# ---------------------------------------------------------------- night window

def night_start(cfg: dict[str, Any], now: datetime | None = None) -> str:
    """Start of the current night = the last report_time (local), as ISO UTC 'Z'.
    'Same night' rules (1 push per coin, no re-audit, 1 pass per mint) use it."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(LOCAL_TZ)
    h, m = (int(x) for x in cfg["report_time"].split(":"))
    start = datetime.combine(local.date(), dtime(h, m), LOCAL_TZ)
    if start > local:
        start -= timedelta(days=1)
    return start.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---------------------------------------------------------------- secrets

def read_secret(name: str) -> str | None:
    """Read a secret file. Never print or log the value."""
    p = SECRETS_DIR / name
    if not p.is_file():
        return None
    v = p.read_text()
    if name == "TG_TOKEN":
        v = "".join(v.split())  # strip ALL whitespace
    else:
        v = v.strip()
    return v or None


# Back-compat names from the STEP 3 stub
SETTINGS: dict[str, Any] = defaults()


def validate_settings(settings: dict[str, Any]) -> dict[str, Any]:
    return {k: coerce(k, v) for k, v in settings.items() if k in SPEC_BY_KEY}


# ---------------------------------------------------------------- PAPER -> LIVE (STEP 11 rule)

def full_paper_night(conn: sqlite3.Connection, now: datetime | None = None, max_gap_min: int = 10) -> bool:
    """True if the db holds at least 1 full PAPER night: a finished night window
    (report_time to report_time) where the desk wrote balance rows the whole time
    (first within 10 min of the start, last within 10 min of the end, no gap over
    10 min) and no LIVE position was opened."""
    cfg = load(conn)
    now = now or datetime.now(timezone.utc)
    end = datetime.fromisoformat(night_start(cfg, now).replace("Z", "+00:00"))
    first = conn.execute("SELECT MIN(ts) FROM balance").fetchone()[0]
    if not first:
        return False
    first_dt = datetime.fromisoformat(first.replace("Z", "+00:00"))
    gap = timedelta(minutes=max_gap_min)
    while end - timedelta(days=1) >= first_dt - timedelta(days=1):
        start = end - timedelta(days=1)
        s, e = (x.isoformat(timespec="milliseconds").replace("+00:00", "Z") for x in (start, end))
        ts = [datetime.fromisoformat(r[0].replace("Z", "+00:00")) for r in
              conn.execute("SELECT ts FROM balance WHERE ts >= ? AND ts < ? ORDER BY ts", (s, e))]
        live = conn.execute("SELECT 1 FROM positions WHERE mode='LIVE' AND opened_at >= ? AND opened_at < ?",
                            (s, e)).fetchone()
        if ts and not live and ts[0] - start <= gap and end - ts[-1] <= gap and \
                all(b - a <= gap for a, b in zip(ts, ts[1:])):
            return True
        end = start
    return False


def set_mode(conn: sqlite3.Connection, mode: str, confirm: str | None = None, who: str = "owner") -> dict:
    """PAPER | LIVE. LIVE needs confirm == "LIVE" and 1 full PAPER night in the db."""
    from db import set_setting

    m = coerce("mode", mode)
    old = load(conn)["mode"]
    if m == "LIVE":
        if confirm != "LIVE":
            raise SettingError('LIVE needs {"confirm":"LIVE"}')
        if not full_paper_night(conn):
            raise SettingError("LIVE needs at least 1 full PAPER night in the db first")
    set_setting(conn, "mode", m, who)
    conn.commit()
    return {"key": "mode", "old": old, "new": m, "message": f"mode {old} -> {m}"}
