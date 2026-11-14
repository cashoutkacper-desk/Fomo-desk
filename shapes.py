"""STEP 7 response shapes, built from the db. ONE source for both transports:

- plan A: api.py returns these as JSON (APP_TOKEN required)
- plan B: publish.py writes the same shapes as JSON files under /d/{DATA_PATH}/

Every FOMO URL in here comes from links.py. No secret is ever read here.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import bus
import config
from agents.risk import exit_line, stop_pct_of
from links import SIGNUP_URL, coin_url, profile_url
from runtime import iso, parse_iso

AGENTS = ("scanner", "auditor", "analyst", "executor", "risk")
RANGES = {"LIVE": timedelta(minutes=30), "1H": timedelta(hours=1), "4H": timedelta(hours=4),
          "1D": timedelta(days=1), "1W": timedelta(weeks=1), "ALL": None}
ACTIVITY_PAGE = 30
LAYER_NAMES = ("Mint authority", "Freeze authority", "Dev wallet", "Top 10 holders", "Liquidity",
               "Pool age", "Bundled wallets", "Delegations")


def _rows(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args)]


def _one(conn, sql, args=()):
    r = conn.execute(sql, args).fetchone()
    return dict(r) if r else None


def _label(conn, mint):
    r = _one(conn, "SELECT name, symbol FROM coins WHERE mint=?", (mint,))
    if r and (r["name"] or r["symbol"]):
        return r["name"] or r["symbol"]
    return f"{mint[:4]}...{mint[-3:]}"


def _r(x, nd=2):
    return None if x is None else round(float(x), nd)


# ------------------------------------------------------------------ balance

def balance_now(conn, cfg) -> float:
    return round(config.current_stack(conn, cfg), 2)


def balance_at_night_start(conn, cfg, now) -> float:
    ns = config.night_start(cfg, now)
    r = _one(conn, "SELECT value FROM balance WHERE ts <= ? ORDER BY ts DESC LIMIT 1", (ns,))
    if r is None:
        r = _one(conn, "SELECT value FROM balance WHERE ts >= ? ORDER BY ts ASC LIMIT 1", (ns,))
    return float(r["value"]) if r and r["value"] else float(cfg["paper_start_stack"])


# ------------------------------------------------------------------ agent cards

def agent_status(conn, name, now) -> dict:
    hb = _one(conn, "SELECT ts FROM heartbeats WHERE agent=?", (name,))
    age = (now - parse_iso(hb["ts"])).total_seconds() if hb else None
    if name in bus.paused:
        status = "paused"
    elif age is not None and age <= 120:
        status = "running"
    else:
        status = "down"
    return {"status": status, "heartbeat": hb["ts"] if hb else None}


def agent_card(conn, cfg, name, now) -> dict:
    ns = config.night_start(cfg, now)
    card: dict[str, Any] = {"name": name.capitalize(), **agent_status(conn, name, now)}
    if name == "scanner":
        n = conn.execute("SELECT COUNT(*) FROM coins WHERE migrated_at >= ?", (ns,)).fetchone()[0]
        feed = []
        for s in _rows(conn, "SELECT * FROM scans WHERE ts >= ? ORDER BY id DESC LIMIT 8", (ns,)):
            feed.append({"mint": s["mint"], "name": _label(conn, s["mint"]), "verdict": s["verdict"],
                         "liquidityUsd": s["liquidity_usd"], "reason": s["reason"], "ts": s["ts"],
                         "link": coin_url(s["mint"], "solana")})
        card.update(color="#64d2ff", count=n, headline=f"{n} pools", feed=feed)
    elif name == "auditor":
        taken = conn.execute("SELECT COUNT(DISTINCT mint) FROM audits WHERE ts >= ?", (ns,)).fetchone()[0]
        rej = conn.execute("SELECT COUNT(DISTINCT mint) FROM audits WHERE ts >= ? AND pass=0", (ns,)).fetchone()[0]
        last = _one(conn, "SELECT mint FROM audits WHERE ts >= ? ORDER BY id DESC LIMIT 1", (ns,))
        layers, result = [], None
        if last:
            a = audit(conn, cfg, last["mint"])
            layers = [{"n": l["n"], "pass": l["pass"]} for l in a["layers"]]
            result = "8/8 → Executor" if a["passed"] else a["reason"]
        card.update(count=taken, headline=f"{taken} taken apart", rejected=rej,
                    rejectedText=f"{rej} rejected", layers=layers, result=result,
                    lastMint=last["mint"] if last else None)
    elif name == "analyst":
        v = _one(conn, "SELECT * FROM verdicts WHERE ts >= ? ORDER BY id DESC LIMIT 1", (ns,))
        card.update(color="#ffd60a", headline="Scoring", score=v["score"] if v else None,
                    scorePass=bool(v and v["score"] is not None and v["score"] >= cfg["analyst.min_score"]),
                    greenFrom=cfg["analyst.min_score"], mode=cfg["analyst_mode"],
                    lastMint=v["mint"] if v else None)
    elif name == "executor":
        n = conn.execute("SELECT COUNT(*) FROM positions WHERE opened_at >= ?", (ns,)).fetchone()[0]
        s = _one(conn, "SELECT * FROM signals WHERE ts >= ? ORDER BY id DESC LIMIT 1", (ns,))
        card.update(color="#00d35a", count=n, headline=f"{n} trades",
                    lastSize=_r(s["size_usd"]) if s else None,
                    lastText=(f"${s['size_usd']:,.2f} · {cfg['executor.stack_pct']:g}% of stack"
                              if s and s["size_usd"] else None),
                    lastStatus=s["status"] if s else None, lastMint=s["mint"] if s else None)
    elif name == "risk":
        open_n = conn.execute("SELECT COUNT(*) FROM positions WHERE closed_at IS NULL").fetchone()[0]
        last = _one(conn, "SELECT exit_reason FROM positions WHERE closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT 1")
        last_txt = None
        if last:
            last_txt = {"trail": "Trailed out", "stop": "Stopped"}.get(last["exit_reason"], last["exit_reason"])
        card.update(color="#ff6b9a", headline="Guarding" if open_n else "Idle", open=open_n,
                    lastExit=last_txt, pills=[{"text": f"stop -{cfg['risk.stop_pct']:g}%", "color": "red"},
                                              {"text": f"trail {cfg['risk.trail_pct']:g}%", "color": "green"}],
                    locked=open_n > 0)
    return card


# ------------------------------------------------------------------ GET /api/state

def state(conn, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    cfg = config.load(conn)
    bal = balance_now(conn, cfg)
    start = balance_at_night_start(conn, cfg, now)
    return {
        "ts": iso(now), "mode": cfg["mode"], "balance": bal,
        "todayPct": round((bal / start - 1) * 100, 2) if start else 0.0,
        "multiplier": round(bal / start, 2) if start else 1.0,
        "nightStart": config.night_start(cfg, now), "nightStartBalance": round(start, 2),
        "unread": 0,
        "agents": [agent_card(conn, cfg, a, now) for a in AGENTS],
    }


# ------------------------------------------------------------------ GET /api/chart

def chart(conn, rng="LIVE", now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    rng = rng.upper()
    if rng not in RANGES:
        raise ValueError(f"range must be one of {', '.join(RANGES)}")
    cfg = config.load(conn)
    since = iso(now - RANGES[rng]) if RANGES[rng] else "0000"
    pts = [{"ts": r["ts"], "value": r["value"]} for r in
           _rows(conn, "SELECT ts, value FROM balance WHERE ts >= ? ORDER BY ts", (since,))]
    marks = []
    for p in _rows(conn, "SELECT * FROM positions WHERE opened_at >= ? OR closed_at >= ? ORDER BY opened_at",
                   (since, since)):
        name = _label(conn, p["mint"])
        if p["opened_at"] >= since:
            marks.append({"ts": p["opened_at"], "kind": "buy", "color": "blue", "text": f"Bought {name}",
                          "mint": p["mint"], "link": coin_url(p["mint"], "solana")})
        if p["closed_at"] and p["closed_at"] >= since:
            pnl = p["pnl_usd"] or 0
            if p["exit_reason"] == "stop":
                txt, color = f"-{stop_pct_of(p):g}% · -${abs(pnl):,.2f}", "red"
            elif pnl >= 0:
                txt, color = f"+${pnl:,.2f}", "green"
            else:
                txt, color = f"-${abs(pnl):,.2f}", "red"
            marks.append({"ts": p["closed_at"], "kind": "exit", "color": color, "text": txt,
                          "mint": p["mint"], "link": coin_url(p["mint"], "solana")})
    return {"range": rng, "start": balance_at_night_start(conn, cfg, now), "points": pts, "marks": marks}


def chart_all(conn, now=None) -> dict:
    return {r: chart(conn, r, now) for r in RANGES}


# ------------------------------------------------------------------ GET /api/activity

def activity(conn, before: int | None = None) -> dict:
    sql, args = "SELECT * FROM activity", ()
    if before:
        sql, args = sql + " WHERE id < ?", (int(before),)
    rows = _rows(conn, sql + " ORDER BY id DESC LIMIT ?", (*args, ACTIVITY_PAGE))
    out = []
    for r in rows:
        out.append({"id": r["id"], "ts": r["ts"], "kind": r["kind"], "title": r["title"],
                    "subtitle": r["subtitle"], "value": r["value"], "mint": r["mint"],
                    "open": "audit" if r["kind"] == "rejected" else "coin",
                    "link": coin_url(r["mint"], "solana") if r["mint"] else None})
    nxt = out[-1]["id"] if len(out) == ACTIVITY_PAGE else None
    return {"rows": out, "nextBefore": nxt}


# ------------------------------------------------------------------ GET /api/coin/{mint}

def candles(conn, mint, minutes=240) -> list[dict]:
    """1-minute OHLC from db.prices; volume = change of DexScreener volume.m5."""
    rows = _rows(conn, "SELECT ts, price, volume_m5 FROM prices WHERE mint=? AND price IS NOT NULL ORDER BY ts",
                 (mint,))
    out: list[dict] = []
    prev_vol = None
    for r in rows:
        t = parse_iso(r["ts"]).replace(second=0, microsecond=0)
        key = iso(t)
        vol = r["volume_m5"]
        dv = max(0.0, vol - prev_vol) if (vol is not None and prev_vol is not None) else 0.0
        if vol is not None:
            prev_vol = vol
        if out and out[-1]["t"] == key:
            c = out[-1]
            c["h"], c["l"], c["c"] = max(c["h"], r["price"]), min(c["l"], r["price"]), r["price"]
            c["v"] = round(c["v"] + dv, 2)
        else:
            out.append({"t": key, "o": r["price"], "h": r["price"], "l": r["price"], "c": r["price"],
                        "v": round(dv, 2)})
    return out[-minutes:]


def coin(conn, mint, now=None, dex: dict | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    cfg = config.load(conn)
    c = _one(conn, "SELECT * FROM coins WHERE mint=?", (mint,)) or {"mint": mint}
    last = _one(conn, "SELECT price FROM prices WHERE mint=? AND price IS NOT NULL ORDER BY ts DESC LIMIT 1", (mint,))
    price = (dex or {}).get("priceUsd") or (last["price"] if last else None)
    a = audit(conn, cfg, mint)
    pos = _one(conn, "SELECT * FROM positions WHERE mint=? AND closed_at IS NULL ORDER BY id DESC LIMIT 1", (mint,))
    age_h = None
    if c.get("migrated_at"):
        age_h = (now - parse_iso(c["migrated_at"])).total_seconds() / 3600
    scan_liq = _one(conn, "SELECT liquidity_usd FROM scans WHERE mint=? ORDER BY id DESC LIMIT 1", (mint,))
    position = None
    if pos:
        left = pos and (parse_iso(pos["opened_at"]) + timedelta(hours=cfg["risk.timeout_h"]) - now)
        position = {
            "sizeUsd": pos["size_usd"], "text": f"${pos['size_usd']:,.2f} in · Executor",
            "entryPrice": pos["entry_price"], "peakPrice": pos["peak_price"],
            "sinceEntryPct": _r((price / pos["entry_price"] - 1) * 100) if price else None,
            "stopLine": pos["stop_price"], "stopPct": stop_pct_of(pos),
            "trailActive": bool(pos["trail_active"]),
            "trailLine": (pos["peak_price"] * (1 - cfg["risk.trail_pct"] / 100)) if pos["trail_active"] else None,
            "exitLine": exit_line(pos, cfg),
            "timeoutLeftMin": max(0, int(left.total_seconds() // 60)),
        }
    return {
        "mint": mint, "name": c.get("name"), "symbol": c.get("symbol"), "chain": "Solana",
        "ageText": None if age_h is None else (f"{int(age_h * 60)}m old" if age_h < 1 else f"{int(age_h)}h old"),
        "shield": f"{sum(1 for l in a['layers'] if l['pass'])}/8" if a["audited"] else None,
        "price": price, "candles": candles(conn, mint),
        "stats": {
            "marketCap": (dex or {}).get("marketCap"),
            "liquidity": (dex or {}).get("liquidityUsd") or (scan_liq or {}).get("liquidity_usd"),
            "volume24h": (dex or {}).get("volumeH24"),
            "holders": c.get("holders") if c.get("holders") is not None else "n/a",
        },
        "position": position,
        "button": "Sell on FOMO" if pos else "Buy on FOMO",
        "coin_url": coin_url(mint, "solana"),
    }


# ------------------------------------------------------------------ GET /api/audit/{mint}

def audit(conn, cfg, mint) -> dict:
    rows = _rows(conn, "SELECT * FROM audits WHERE mint=? ORDER BY id DESC LIMIT 8", (mint,))
    rows.sort(key=lambda r: r["layer"])
    by_n = {r["layer"]: r for r in rows}
    layers = []
    for n in range(1, 9):
        r = by_n.get(n)
        layers.append({"n": n, "name": LAYER_NAMES[n - 1],
                       "pass": None if r is None or r["pass"] is None else bool(r["pass"]),
                       "value": r["value_text"] if r else "not checked",
                       "source": r["source"] if r else None})
    red = next((l for l in layers if l["pass"] is False), None)
    audited = bool(rows)
    passed = audited and red is None and all(l["pass"] for l in layers)
    name = _label(conn, mint)
    sig = _one(conn, "SELECT size_usd FROM signals WHERE mint=? ORDER BY id DESC LIMIT 1", (mint,))
    if not audited:
        head, sub = "Not taken apart yet", None
    elif passed:
        head = "Cleared · 8/8 parts"
        sub = f"Sent to Executor · ${sig['size_usd']:,.2f}" if sig and sig["size_usd"] else "Sent to Executor"
    else:
        head, sub = f"Rejected · {red['value'] if red else 'incomplete'}", f"Grok Bot won't buy {name}"
    return {"mint": mint, "name": name, "audited": audited, "passed": passed,
            "reason": red["value"] if red else None, "layers": layers, "headline": head, "subline": sub,
            "enabled": cfg["auditor.enabled"], "coin_url": coin_url(mint, "solana")}


# ------------------------------------------------------------------ GET /api/agents/{name}/decisions

def decisions(conn, name, limit=20) -> dict:
    name = name.lower()
    if name not in AGENTS:
        raise ValueError(f"agent must be one of {', '.join(AGENTS)}")
    out = []
    if name == "scanner":
        for r in _rows(conn, "SELECT * FROM scans ORDER BY id DESC LIMIT ?", (limit,)):
            out.append((r["ts"], r["mint"], r["verdict"], r["reason"]))
    elif name == "auditor":
        for r in _rows(conn, "SELECT mint, MAX(id) id, MAX(ts) ts, MIN(COALESCE(pass,1)) ok FROM audits "
                             "GROUP BY mint ORDER BY id DESC LIMIT ?", (limit,)):
            red = _one(conn, "SELECT value_text FROM audits WHERE mint=? AND pass=0 ORDER BY id DESC LIMIT 1",
                       (r["mint"],))
            out.append((r["ts"], r["mint"], "pass" if r["ok"] else "reject", red["value_text"] if red else "8/8"))
    elif name == "analyst":
        for r in _rows(conn, "SELECT * FROM verdicts ORDER BY id DESC LIMIT ?", (limit,)):
            out.append((r["ts"], r["mint"], "pass" if r["pass"] else "reject",
                        f"score {r['score']:g}" if r["pass"] else r["reason"]))
    elif name == "executor":
        for r in _rows(conn, "SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)):
            out.append((r["ts"], r["mint"], r["status"],
                        f"${r['size_usd']:,.2f}" if r["size_usd"] else None))
    else:
        for r in _rows(conn, "SELECT * FROM positions WHERE exit_reason IS NOT NULL ORDER BY id DESC LIMIT ?",
                       (limit,)):
            out.append((r["closed_at"] or r["opened_at"], r["mint"], r["exit_reason"],
                        f"{'+' if (r['pnl_usd'] or 0) >= 0 else '-'}${abs(r['pnl_usd'] or 0):,.2f}"))
    return {"agent": name, "decisions": [
        {"ts": ts, "mint": m, "coin": _label(conn, m), "verdict": v, "reason": why,
         "link": coin_url(m, "solana")} for ts, m, v, why in out]}


# ------------------------------------------------------------------ GET /api/account, /api/settings

def account(conn, telegram_connected: bool = False) -> dict:
    cfg = config.load(conn)
    handle = cfg.get("fomo_handle") or ""
    return {"mode": cfg["mode"], "stack": balance_now(conn, cfg), "liveStack": cfg["live_stack"],
            "fomoHandle": handle, "profile_url": profile_url(handle) if handle else None,
            "signup_url": SIGNUP_URL, "telegram": {"connected": telegram_connected},
            "fullPaperNight": config.full_paper_night(conn)}


def settings(conn) -> dict:
    cfg = config.load(conn)
    groups: dict[str, list] = {}
    for s in config.SPECS:
        groups.setdefault(s.group, []).append({
            "key": s.key, "value": cfg[s.key], "kind": s.kind, "min": s.lo, "max": s.hi,
            "choices": list(s.choices) or None, "step10": s.step10})
    return {"groups": groups}
