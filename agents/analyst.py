"""AGENT 3 · ANALYST. Never decides itself: builds a numbers-only state, asks Jev
4 closed "noul" questions (or computes them in RULES MODE), and code applies the gate.

Output -> db.verdicts + pubsub "verdict"; passed -> bus.analyst_to_executor;
reject -> Activity "Rejected X / Analyst · {first failed rule} / Skipped"
("Analyst (rules) · ..." in rules mode).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import Any

import bus
from notifier import push_reject
import logutil
from config import read_secret
from runtime import Ctx, coin_label
from sources import RateLimited

log = logutil.get("analyst")

QUESTIONS = {
    "alive_6h": {"type": "noul", "instructions": "This coin is still actively traded 6 hours from now"},
    "organic": {"type": "noul", "instructions": "Most buyers in the last 30 minutes are separate real wallets"},
    "flush": {"type": "noul", "instructions": "Price just flushed down and buyers are absorbing it"},
    "chase": {"type": "noul", "instructions": "This entry is chasing a green candle that already ran"},
}
NAMES = tuple(QUESTIONS)


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _num(x: Any, nd: int = 2) -> str:
    if x is None:
        return "n/a"
    try:
        f = float(x)
    except (TypeError, ValueError):
        return "n/a"
    return f"{f:,.0f}" if abs(f) >= 100 else f"{round(f, nd):g}"


_BUY_WORD = re.compile(r"\bbuy\b", re.IGNORECASE)


def build_state(name: str, age_min: float, dex: dict, auditor: dict, max_chars: int) -> str:
    """Plain text, numbers only, no opinions. No thresholds, no balance, no word 'buy'."""
    vals = {l["n"]: l.get("value") for l in auditor.get("layers", [])}
    top10 = (vals.get(4) or "n/a").rstrip("%")
    dev = (vals.get(3) or "n/a").split("%")[0]
    safe_name = _BUY_WORD.sub("b-y", name or "unknown")
    s = (
        f"Coin {safe_name} on Solana, graduated {int(age_min)} min ago. "
        f"Liquidity ${_num(dex.get('liquidityUsd'))}. Market cap ${_num(dex.get('marketCap'))}. "
        f"Last 5 min: {_num(dex.get('buysM5'))} buys, {_num(dex.get('sellsM5'))} sells, "
        f"volume ${_num(dex.get('volumeM5'))}, price {_num(dex.get('changeM5'))}%. "
        f"Last 1h: {_num(dex.get('buysH1'))} buys, {_num(dex.get('sellsH1'))} sells, "
        f"volume ${_num(dex.get('volumeH1'))}, price {_num(dex.get('changeH1'))}%. "
        f"Top 10 hold {top10}%. Dev holds {dev}%, sold 0. LP {vals.get(5) or 'n/a'}. "
        f"Bundled {vals.get(7) or 'n/a'}. Delegations {vals.get(8) or 'n/a'}."
    )
    return s[:max_chars]


def rules_answers(dex: dict, bundled: int, cfg: dict) -> dict[str, float]:
    """RULES MODE: same 4 names, same 0-1 range, computed from DexScreener."""
    vol_h1 = float(dex.get("volumeH1") or 0)
    buys_h1 = float(dex.get("buysH1") or 0)
    m5 = float(dex.get("changeM5") or 0)
    h1 = float(dex.get("changeH1") or 0)
    buys_m5 = float(dex.get("buysM5") or 0)
    sells_m5 = float(dex.get("sellsM5") or 0)
    alive = clamp(vol_h1 / cfg["analyst.rules.alive_volume_h1"])
    organic = clamp(buys_h1 / cfg["analyst.rules.organic_buys_h1"])
    if bundled >= cfg["analyst.rules.bundled_min_wallets"]:
        organic *= cfg["analyst.rules.bundled_penalty"]
    if m5 <= -cfg["analyst.rules.flush_drop_m5_pct"] and buys_m5 >= sells_m5:
        flush = 1.0
    elif m5 < 0:
        flush = 0.5
    else:
        flush = 0.0
    chase = clamp(h1 / cfg["analyst.rules.chase_h1_pct"])
    return {"alive_6h": alive, "organic": organic, "flush": flush, "chase": chase}


def gate(a: dict[str, float], cfg: dict) -> tuple[int, bool, list[str]]:
    """Code-only gate. Jev never sees it. -> (score, passed, failed rules in order)."""
    score = round(100 * (a["alive_6h"] + a["organic"] + a["flush"] + (1 - a["chase"])) / 4 + 1e-9)
    failed = []
    if a["alive_6h"] < cfg["analyst.min_alive_6h"] - 1e-9:
        failed.append(f"alive_6h {a['alive_6h']:.2f}")
    if a["organic"] < cfg["analyst.min_organic"] - 1e-9:
        failed.append(f"organic {a['organic']:.2f}")
    if a["chase"] > cfg["analyst.max_chase"] + 1e-9:
        failed.append(f"chase {a['chase']:.2f}")
    if score < cfg["analyst.min_score"]:
        failed.append(f"score {score}")
    return score, not failed, failed


def bundled_count(auditor: dict) -> int:
    for l in auditor.get("layers", []):
        if l["n"] == 7 and l.get("pass") is not None:
            m = re.match(r"(\d+)", str(l.get("value")))
            return int(m.group(1)) if m else 0
    return 0


class Analyst:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.scored = 0

    async def ask_jev(self, state: str, cfg: dict) -> tuple[dict | None, str, int, str | None]:
        """-> (answers or None, model, input tokens, reject reason)."""
        src = self.ctx.src
        timeout = cfg["analyst.timeout_s"]
        delay = 0 if self.ctx.fast else cfg["analyst.retry_delay_s"]
        deadline_429 = asyncio.get_running_loop().time() + cfg["analyst.queue_429_s"]
        attempts_err = 0
        while True:
            try:
                resp = await asyncio.wait_for(src.jev.ask(state, QUESTIONS, timeout), timeout + 1)
                break
            except RateLimited:
                if asyncio.get_running_loop().time() >= deadline_429:
                    return None, "", 0, "no answer"
                await asyncio.sleep(0 if self.ctx.fast else 2)
            except Exception as e:  # noqa: BLE001  HTTP error / timeout / no key
                attempts_err += 1
                log.info(f"jev_error\t{type(e).__name__}: {e}")
                if attempts_err > 1:
                    return None, "", 0, "no answer"
                await asyncio.sleep(delay)
        model = str(resp.get("model") or "")
        usage = resp.get("usage") or {}
        tokens = int(usage.get("input_tokens") or 0)
        answers = {}
        for n in NAMES:
            try:
                v = float(resp["answers"][n]["noul"])
            except (KeyError, TypeError, ValueError):
                v = None
            if v is None or not (0.0 <= v <= 1.0):
                log.info(f"bad_answer\traw={json.dumps(resp)[:2000]}")
                return None, model, tokens, "bad answer"
            answers[n] = v
        return answers, model, tokens, None

    async def handle(self, msg: dict) -> dict | None:
        ctx, conn = self.ctx, self.ctx.conn
        cfg = ctx.cfg()
        scan, aud = msg["scanner"], msg["auditor"]
        mint = scan["mint"]
        if conn.execute("SELECT 1 FROM verdicts WHERE mint=? AND ts>=?", (mint, ctx.night_start())).fetchone():
            log.info(f"{mint}\tskip\talready scored tonight")
            return None  # never re-asks the same coin
        mode = cfg["analyst_mode"]
        rules = mode == "rules"
        try:
            views = await ctx.src.dex.tokens([mint])
            dex = views.get(mint)
        except Exception as e:  # noqa: BLE001
            log.info(f"{mint}\tdex_error\t{type(e).__name__}")
            dex = None
        coin = conn.execute("SELECT migrated_at FROM coins WHERE mint=?", (mint,)).fetchone()
        from runtime import parse_iso
        age = (ctx.now() - parse_iso(coin[0])).total_seconds() / 60 if coin and coin[0] else scan.get("ageMin", 0)
        state = build_state(scan.get("name") or coin_label(conn, mint), age, dex or {}, aud,
                            cfg["analyst.max_state_chars"])
        state_hash = hashlib.sha256(state.encode()).hexdigest()[:12]
        answers, model, tokens, reason = None, "rules" if rules else "", 0, None
        if dex is None:
            reason = "no answer"  # no fresh DexScreener read = no state
        elif rules:
            answers = rules_answers(dex, bundled_count(aud), cfg)
        else:
            answers, model, tokens, reason = await self.ask_jev(state, cfg)
        if answers is not None:
            score, passed, failed = gate(answers, cfg)
        else:
            score, passed, failed = 0, False, [reason]
        self.scored += 1
        out = {"agent": "analyst", "mint": mint,
               "answers": {k: round(v, 4) for k, v in (answers or {}).items()},
               "score": score, "passed": passed, "failed": failed, "model": model,
               "inputTokens": tokens}
        ts = ctx.now_iso()
        a = answers or {}
        conn.execute(
            "INSERT INTO verdicts(mint, ts, alive_6h, organic, flush, chase, score, pass, reason, input_tokens)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (mint, ts, a.get("alive_6h"), a.get("organic"), a.get("flush"), a.get("chase"), score,
             int(passed), None if passed else failed[0], tokens),
        )
        if not passed:
            who = "Analyst (rules)" if rules else "Analyst"
            conn.execute(
                "INSERT INTO activity(ts, kind, mint, agent, title, subtitle, value) VALUES (?,?,?,?,?,?,?)",
                (ts, "rejected", mint, "analyst", f"Rejected {coin_label(conn, mint)}",
                 f"{who} · {failed[0]}", "Skipped"),
            )
        conn.commit()
        if not passed:
            await push_reject(ctx.notifier, cfg, mint, coin_label(conn, mint),
                              "Analyst (rules)" if rules else "Analyst", str(failed[0]))
        nums = " ".join(f"{k}={a.get(k)}" for k in NAMES)
        log.info(f"{mint}\tstate {state_hash}\t{nums}\tscore {score}\t"
                 f"{'pass' if passed else 'reject: ' + failed[0]}\ttokens {tokens}\tmodel {model}")
        bus.publish("verdict", out)
        if passed:
            await bus.analyst_to_executor.put(
                {"scanner": scan, "auditor": aud, "analyst": out,
                 "priceAtAnalyst": (dex or {}).get("priceUsd")})
        return out

    async def run(self) -> None:
        while True:
            bus.beat("analyst")
            try:
                msg = await asyncio.wait_for(bus.auditor_to_analyst.get(), timeout=15)
            except asyncio.TimeoutError:
                continue
            while bus.is_paused("analyst"):
                bus.beat("analyst")
                await asyncio.sleep(2)
            try:
                await self.handle(msg)
            except Exception as e:  # noqa: BLE001
                log.info(f"{msg.get('scanner', {}).get('mint')}\terror\t{type(e).__name__}: {e}")


def jev_key() -> str | None:
    """TYPESAFE_API_KEY from secrets/ (whitespace stripped). Never printed or logged."""
    return read_secret("TYPESAFE_API_KEY")


async def run(ctx: Ctx) -> None:
    await Analyst(ctx).run()
