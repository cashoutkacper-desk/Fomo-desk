"""STEP 12 · night-report (server side of the daily routine).

  desk-night-report export            -> JSON for the night that just ended (no secrets)
  desk-night-report finalize <url>    -> store last_report_url, Telegram the owner
                                         "Morning report: <url>", then clear db.skips
The routine builds the Page from `export`; every coin in it links with its
coin_url (links.py, r=savipww) and the footer is "New to FOMO? " + SIGNUP_URL.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import config
import db
from links import FOMO_HOST, SIGNUP_URL, coin_url, is_valid_fomo_url
from runtime import iso, parse_iso

FOOTER = "New to FOMO? " + SIGNUP_URL
NIGHT_LOOKBACK_H = 20   # run at/after report_time -> the night is the previous report_time .. now


def night_window(cfg: dict, now: datetime) -> tuple[str, str]:
    """Start = the report_time at least 20 h before now (so a run at 8:00, or a
    late run at 1 PM, covers the whole night since yesterday's 8:00); end = now."""
    return config.night_start(cfg, now - timedelta(hours=NIGHT_LOOKBACK_H)), iso(now)


def _local(ts: str | None) -> str | None:
    if not ts:
        return None
    return parse_iso(ts).astimezone(config.LOCAL_TZ).strftime("%Y-%m-%d %H:%M %Z")


def kill_group(reason: str | None) -> str:
    r = (reason or "").strip()
    if r.startswith("liq"):
        return "liquidity too low"
    if r.startswith("top 10 hold"):
        return "top 10 hold too much"
    if re.match(r"^\+\d", r):
        return "already pumped in 1h"
    if r.startswith("copycat"):
        return "copycat"
    return r or "unknown"


class Coins:
    def __init__(self, conn: sqlite3.Connection):
        self.conn, self.seen = conn, {}

    def ref(self, mint: str) -> dict:
        if mint not in self.seen:
            row = self.conn.execute("SELECT name, symbol FROM coins WHERE mint=?", (mint,)).fetchone()
            self.seen[mint] = {"mint": mint, "name": row["name"] if row else None,
                               "symbol": row["symbol"] if row else None, "coin_url": coin_url(mint, "solana")}
        return dict(self.seen[mint])


def _balance_at(conn, cfg, ts: str) -> float:
    row = conn.execute("SELECT value FROM balance WHERE ts<=? ORDER BY ts DESC, rowid DESC LIMIT 1", (ts,)).fetchone()
    if row is not None:
        return round(row[0], 2)
    return round(float(cfg["paper_start_stack"]), 2)


def export(conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    cfg = config.load(conn)
    start, end = night_window(cfg, now)
    coins = Coins(conn)

    trades = []
    for p in conn.execute("SELECT * FROM positions WHERE closed_at>=? AND closed_at<=? ORDER BY closed_at",
                          (start, end)):
        pnl = round(p["pnl_usd"] or 0, 2)
        trades.append({**coins.ref(p["mint"]), "mode": p["mode"],
                       "entry": p["entry_price"], "exit": p["exit_price"], "size_usd": p["size_usd"],
                       "pnl_usd": pnl, "pnl_pct": round(pnl / p["size_usd"] * 100, 2) if p["size_usd"] else None,
                       "fees_usd": round(p["fees_usd"] or 0, 2), "rule": p["exit_reason"],
                       "opened_at": p["opened_at"], "closed_at": p["closed_at"],
                       "opened_local": _local(p["opened_at"]), "closed_local": _local(p["closed_at"])})
    red = sorted([t for t in trades if t["pnl_usd"] < 0], key=lambda t: t["pnl_usd"])
    green = sorted([t for t in trades if t["pnl_usd"] >= 0], key=lambda t: -t["pnl_usd"])
    still_open = [{**coins.ref(p["mint"]), "entry": p["entry_price"], "size_usd": p["size_usd"],
                   "opened_at": p["opened_at"], "opened_local": _local(p["opened_at"])}
                  for p in conn.execute("SELECT * FROM positions WHERE closed_at IS NULL ORDER BY opened_at")]

    seen = [r[0] for r in conn.execute(
        "SELECT DISTINCT mint FROM scans WHERE ts>=? AND ts<=? ORDER BY mint", (start, end))]

    kills: dict[str, dict] = {}
    for s in conn.execute("SELECT mint, ts, reason FROM scans WHERE verdict='kill' AND ts>=? AND ts<=? ORDER BY ts",
                          (start, end)):
        g = kills.setdefault(kill_group(s["reason"]), {"reason": kill_group(s["reason"]), "count": 0, "coins": []})
        g["count"] += 1
        g["coins"].append({**coins.ref(s["mint"]), "detail": s["reason"], "ts": s["ts"], "local": _local(s["ts"])})
    kill_list = sorted(kills.values(), key=lambda g: -g["count"])

    refusals: dict[int, dict] = {}
    for a in conn.execute("SELECT mint, ts, layer, name, value_text, source FROM audits"
                          " WHERE pass=0 AND ts>=? AND ts<=? ORDER BY layer, ts", (start, end)):
        g = refusals.setdefault(a["layer"], {"layer": a["layer"], "name": a["name"], "count": 0, "coins": []})
        g["count"] += 1
        g["coins"].append({**coins.ref(a["mint"]), "value": a["value_text"], "source": a["source"],
                           "ts": a["ts"], "local": _local(a["ts"])})

    rejects = [{**coins.ref(v["mint"]), "failed_rule": v["reason"], "score": v["score"],
                "ts": v["ts"], "local": _local(v["ts"])}
               for v in conn.execute("SELECT mint, ts, score, reason FROM verdicts"
                                     " WHERE pass=0 AND ts>=? AND ts<=? ORDER BY ts", (start, end))]

    jev = conn.execute("SELECT COUNT(*), COALESCE(SUM(input_tokens),0) FROM verdicts"
                       " WHERE ts>=? AND ts<=? AND input_tokens>0", (start, end)).fetchone()
    for m in seen:
        coins.ref(m)
    out = {
        "mode": cfg["mode"],
        "night": {"start": start, "end": end, "start_local": _local(start), "end_local": _local(end),
                  "report_time": cfg["report_time"]},
        "balance": {"start": _balance_at(conn, cfg, start), "end": _balance_at(conn, cfg, end)},
        "trades": {"count": len(trades), "red": red, "green": green, "open": still_open},
        "coins_seen": len(seen),
        "scanner_kills": {"count": sum(g["count"] for g in kill_list), "by_reason": kill_list},
        "auditor_refusals": {"count": sum(g["count"] for g in refusals.values()),
                             "by_layer": [refusals[k] for k in sorted(refusals)]},
        "analyst_rejects": {"count": len(rejects), "items": rejects},
        "jev": {"calls": jev[0], "input_tokens": jev[1], "output_tokens": None,
                "cost_usd": None,
                "note": "the desk records Jev input tokens only and has no Jev price list: output tokens and cost unknown"},
        "fees_usd": round(sum(t["fees_usd"] for t in trades), 2),
        "pnl_usd": round(sum(t["pnl_usd"] for t in trades), 2),
        "empty": not trades and not seen,
        "coins": sorted(coins.seen.values(), key=lambda c: (c["name"] or "", c["mint"])),
        "signup_url": SIGNUP_URL,
        "footer": FOOTER,
    }
    return out


async def finalize(conn: sqlite3.Connection, notifier: Any, page_url: str) -> dict:
    """Order matters: validate -> store last_report_url -> Telegram -> clear skips
    (only when the Telegram send succeeded)."""
    url = (page_url or "").strip()
    if not re.match(r"^https://[^\s/]+\.[^\s/]+(/\S*)?$", url):
        raise ValueError("page_url must be an https URL")
    if FOMO_HOST in url.lower() and not is_valid_fomo_url(url):
        raise ValueError("a FOMO URL must come from links.py (r=savipww)")
    db.set_setting(conn, "last_report_url", url, "night-report")
    conn.commit()
    sent = await notifier.message(f"Morning report: {url}", kind="report")
    cleared = 0
    if sent:
        cleared = conn.execute("DELETE FROM skips").rowcount
        conn.commit()
    return {"ok": bool(sent), "stored": True, "sent": bool(sent), "skips_cleared": cleared}


def main(argv: list[str]) -> int:
    if len(argv) >= 1 and argv[0] == "export":
        conn = db.connect()
        print(json.dumps(export(conn), ensure_ascii=False, indent=1))
        return 0
    if len(argv) == 2 and argv[0] == "finalize":
        import httpx

        import notifier as notifier_mod
        conn = db.connect()
        cfg = config.load(conn)

        async def go():
            async with httpx.AsyncClient() as client:
                return await finalize(conn, notifier_mod.build(cfg, client), argv[1])
        try:
            res = asyncio.run(go())
        except ValueError as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            return 2
        print(json.dumps(res))
        return 0 if res["ok"] else 1
    print("usage: desk-night-report export | finalize <https page url>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
