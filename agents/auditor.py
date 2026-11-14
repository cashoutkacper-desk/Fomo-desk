"""AGENT 2 · AUDITOR. Takes every coin Scanner passes apart, 8 layers, in order.
One red layer and the coin is dead. 7 of 8 is a no. No data is never a pass.

Output -> db.audits (1 row per layer), pubsub "audit_layer" per layer + "audit",
passed -> bus.auditor_to_analyst, red -> Activity "Rejected X / Auditor · {reason} / Skipped".
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

import bus
import fields
import logutil
from runtime import Ctx, coin_label, parse_iso, short_addr
from sources import RC_SRC, RPC_SRC, bonding_curve_address, top10_pct

log = logutil.get("auditor")
GRAPH_SRC = "rugcheck_insiders_nonempty.json"

LAYER_NAMES = {
    1: "Mint authority",
    2: "Freeze authority",
    3: "Dev wallet",
    4: "Top 10 holders",
    5: "Liquidity",
    6: "Pool age",
    7: "Bundled wallets",
    8: "Delegations",
}
NO_DATA_NAME = {1: "mint", 2: "freeze", 3: "dev", 4: "top 10", 5: "liquidity", 6: "pool age",
                7: "bundles", 8: "delegations"}


def market_liq_paths() -> list[str]:
    return [e["path"].split("].", 1)[1] for e in fields.FIELD_MAP["liquidity_usd"]
            if e["source"] == RC_SRC and e["path"].startswith("markets[0].")]


class NoData(Exception):
    """Source failed after retries / key missing -> red 'no data: {layer}'."""


def _graph_key(field: str) -> str:
    """Last segment of a FIELD_MAP graph path: '[].links' -> 'links', '[].nodes[].id' -> 'id'."""
    return fields.path_for(field, GRAPH_SRC).split("[].")[-1]


def biggest_group(graph: Any) -> int | None:
    """Layer 7. Wallets = nodes holding this mint; edges = links between them.
    Bundled = size of the biggest connected group. None = no links key anywhere."""
    nets = graph if isinstance(graph, list) else ([graph] if isinstance(graph, dict) else [])
    k_nodes, k_links = _graph_key("insiders_graph_nodes"), _graph_key("insiders_graph_links")
    k_id, k_hold = _graph_key("insiders_graph_node_id"), _graph_key("insiders_graph_node_holdings")
    k_src, k_dst = _graph_key("insiders_graph_link_source"), _graph_key("insiders_graph_link_target")
    has_links = False
    holders: set[str] = set()
    edges: list[tuple[str, str]] = []
    for net in nets:
        if not isinstance(net, dict) or k_links not in net:
            continue
        has_links = True
        for n in net.get(k_nodes) or []:
            try:
                if n.get(k_id) and float(n.get(k_hold) or 0) > 0:
                    holders.add(n[k_id])
            except (TypeError, ValueError, AttributeError):
                pass
        for l in net.get(k_links) or []:
            if isinstance(l, dict):
                edges.append((l.get(k_src), l.get(k_dst)))
    if not has_links:
        return None
    parent = {h: h for h in holders}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        if a in holders and b in holders:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb
    sizes: dict[str, int] = {}
    for h in holders:
        r = find(h)
        sizes[r] = sizes.get(r, 0) + 1
    groups = [s for s in sizes.values() if s >= 2]
    return max(groups) if groups else 0


def delegation_scan(accounts: list[dict]) -> tuple[int, str | None, int]:
    """Layer 8. -> (biggest delegate count, that delegate, holders with balance > 0)."""
    per: dict[str, int] = {}
    holders = 0
    for acc in accounts:
        try:
            info = acc["account"]["data"]["parsed"]["info"]
        except (KeyError, TypeError):
            continue
        amt = int((info.get("tokenAmount") or {}).get("amount") or 0)
        if amt > 0:
            holders += 1
            d = info.get("delegate")
            if d:
                per[d] = per.get(d, 0) + 1
    if not per:
        return 0, None, holders
    d, n = max(per.items(), key=lambda kv: kv[1])
    return n, d, holders


class Auditor:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.taken_apart = 0
        self.rejected = 0
        self.layer8_down_notified = False
        self.layer8_kills = 0

    async def _retry(self, layer: int, fn: Callable[[], Awaitable[Any]], cfg: dict) -> Any:
        tries = cfg["auditor.retries"]
        delay = 0 if self.ctx.fast else cfg["auditor.retry_delay_s"]
        last = None
        for i in range(tries + 1):
            try:
                return await fn()
            except Exception as e:  # noqa: BLE001
                last = e
                if i < tries:
                    await asyncio.sleep(delay)
        log.info(f"source_failed\tlayer {layer}\t{type(last).__name__}: {last}")
        raise NoData(NO_DATA_NAME[layer])

    async def audit(self, scan: dict, forward: bool = True) -> dict:
        """forward=False: an on-demand audit (STEP 9 "buy X" for a coin never pushed).
        The 8 layers are written to db.audits, but nothing goes to Analyst and no
        Activity row is written: "not sized, not on the desk"."""
        ctx, conn = self.ctx, self.ctx.conn
        cfg = ctx.cfg()
        mint = scan["mint"]
        pool = scan.get("pool")
        coin = conn.execute(
            "SELECT creator, creator_balance_at_migration, migrated_at FROM coins WHERE mint=?", (mint,)
        ).fetchone()
        creator, creator_bal0, migrated_at = (coin[0], coin[1], coin[2]) if coin else (None, None, None)
        self.taken_apart += 1
        bus.publish("audit_start", {"mint": mint})
        cache: dict[str, Any] = {}

        async def acct():
            if "acct" not in cache:
                cache["acct"] = await self._retry(1, lambda: ctx.src.rpc.account_info(mint), cfg)
            return cache["acct"]

        async def report(layer):
            if "report" not in cache:   # fetched once per audit, reused
                cache["report"] = await self._retry(layer, lambda: ctx.src.rugcheck.report(mint), cfg)
            return cache["report"]

        async def graph(layer):
            if "graph" not in cache:
                cache["graph"] = await self._retry(layer, lambda: ctx.src.rugcheck.graph(mint), cfg)
            return cache["graph"]

        async def supply():
            a = await acct()
            raw = fields.get(a, "token_supply", RPC_SRC)
            try:
                return int(raw)
            except (TypeError, ValueError):
                return 0

        holders_out: int | None = None

        async def layer(n: int) -> tuple[bool, str, str]:
            nonlocal holders_out
            if n in (1, 2):
                a = await acct()
                if fields.resolve(a, "result.value") is None:
                    raise NoData(NO_DATA_NAME[n])
                field = "mint_authority" if n == 1 else "freeze_authority"
                auth = fields.get(a, field, RPC_SRC)
                if auth is None:
                    return True, "Revoked", "rpc"
                return False, "mint not revoked" if n == 1 else "freeze not revoked", "rpc"
            if n == 3:
                sup = await supply()
                if sup <= 0:
                    return False, "no supply", "rpc"
                if creator is None or creator_bal0 is None:
                    return False, "no data: dev", "rpc"
                bal = await self._retry(3, lambda: ctx.src.rpc.owner_balance(creator, mint), cfg)
                pct = bal / sup * 100
                if pct >= cfg["auditor.max_dev_pct"]:
                    return False, f"dev holds {pct:.0f}%", "rpc"
                if bal < int(creator_bal0):
                    return False, "dev sold", "rpc"
                return True, f"{pct:.1f}% · sold 0", "rpc"
            if n == 4:
                sup = await supply()
                excl = {x for x in (pool, bonding_curve_address(mint)) if x}
                pct = await self._retry(4, lambda: top10_pct(ctx.src.rpc, mint, excl, sup, log), cfg)
                if pct >= cfg["auditor.max_top10_pct"]:
                    return False, f"top 10 hold {pct:.0f}%", "rpc"
                return True, f"{pct:.1f}%", "rpc"
            if n == 5:
                r = await report(5)
                markets = fields.get(r, "markets", RC_SRC) or []
                pk_path = fields.path_for("pool_address", RC_SRC).split("].", 1)[1]  # "pubkey"
                market = next((m for m in markets if fields.resolve(m, pk_path) == pool), None)
                if market is None:
                    return False, "no data: liquidity", "rugcheck"
                locked = fields.resolve(market, fields.subpath("lp_locked_pct", RC_SRC, "markets"))
                if locked is None:
                    return False, "no data: liquidity", "rugcheck"
                if float(locked) < cfg["auditor.min_lp_locked_pct"]:
                    return False, "liquidity unlocked", "rugcheck"
                # markets[0].lp.quoteUSD + markets[0].lp.baseUSD (fields.py liquidity_usd)
                liq_now = sum(float(fields.resolve(market, p) or 0) for p in market_liq_paths())
                liq_scan = scan.get("liquidityUsd") or 0
                if liq_scan and liq_now < liq_scan * (1 - cfg["auditor.max_liq_drop_pct"] / 100):
                    return False, "liquidity pulled", "rugcheck"
                cur = fields.resolve(market, fields.subpath("lp_current_supply", RC_SRC, "markets"))
                word = "burned" if cur in (0, "0") else "locked"
                return True, f"{float(locked):.0f}% {word}", "rugcheck"
            if n == 6:
                if not migrated_at:
                    return False, "no data: pool age", "rpc"
                age = (ctx.now() - parse_iso(migrated_at)).total_seconds() / 60
                if age < cfg["auditor.min_pool_age_min"]:
                    return False, "pool too young", "rpc"
                return True, (f"{int(age)}m" if age < 120 else f"{age / 60:.1f}h"), "rpc"
            if n == 7:
                g = await graph(7)
                size = biggest_group(g)
                if size is None:
                    return False, "no data: bundles", "rugcheck"
                if size >= cfg["auditor.max_bundled"]:
                    return False, f"{size} bundled wallets", "rugcheck"
                return True, ("None" if size == 0 else f"{size} wallets"), "rugcheck"
            if n == 8:
                try:
                    accs = await self._retry(8, lambda: ctx.src.rpc.program_accounts(mint), cfg)
                except NoData:
                    self.layer8_kills += 1
                    if not self.layer8_down_notified:
                        self.layer8_down_notified = True
                        await ctx.notifier.message(
                            "layer 8 down, every coin is red until rpc_url changes", kind="alert")
                    raise
                self.layer8_down_notified = False
                count, delegate, holders = delegation_scan(accs)
                holders_out = holders
                conn.execute("UPDATE coins SET holders=? WHERE mint=?", (holders, mint))
                if count > cfg["auditor.max_delegated_accounts"]:
                    return False, f"{count} wallets → {short_addr(delegate)}", "rpc"
                return True, "None" if count == 0 else f"{count} wallets", "rpc"
            raise ValueError(n)

        layers: list[dict] = []
        passed, reason, checked = True, None, 0
        for n in range(1, 9):
            t0 = time.monotonic()
            bus.publish("audit_layer", {"mint": mint, "n": n, "name": LAYER_NAMES[n], "state": "reading"})
            try:
                ok, value, source = await layer(n)
            except NoData as e:
                ok, value, source = False, f"no data: {e}", "rpc" if n in (1, 2, 3, 4, 6, 8) else "rugcheck"
            ms = int((time.monotonic() - t0) * 1000)
            checked = n
            row = {"n": n, "name": LAYER_NAMES[n], "pass": ok, "value": value, "source": source}
            layers.append(row)
            ts = ctx.now_iso()
            conn.execute(
                "INSERT INTO audits(mint, ts, layer, name, value_text, pass, source) VALUES (?,?,?,?,?,?,?)",
                (mint, ts, n, LAYER_NAMES[n], value, int(ok), source),
            )
            conn.commit()
            log.info(f"{mint}\tlayer {n}\t{LAYER_NAMES[n]}\t{'ok' if ok else 'red'}\t{value}\t{ms}ms")
            bus.publish("audit_layer", {"mint": mint, **row, "ms": ms})
            if not ok:
                passed, reason = False, value
                break
        for n in range(checked + 1, 9):
            layers.append({"n": n, "name": LAYER_NAMES[n], "pass": None, "value": "not checked",
                           "source": None})
            conn.execute(
                "INSERT INTO audits(mint, ts, layer, name, value_text, pass, source) VALUES (?,?,?,?,?,?,?)",
                (mint, ctx.now_iso(), n, LAYER_NAMES[n], "not checked", None, None),
            )
        out = {"agent": "auditor", "mint": mint, "ts": ctx.now_iso(), "passed": passed,
               "layersChecked": checked, "layers": layers, "holders": holders_out, "reason": reason}
        if not passed and forward:
            self.rejected += 1
            conn.execute(
                "INSERT INTO activity(ts, kind, mint, agent, title, subtitle, value) VALUES (?,?,?,?,?,?,?)",
                (out["ts"], "rejected", mint, "auditor", f"Rejected {coin_label(conn, mint)}",
                 f"Auditor · {reason}", "Skipped"),
            )
        conn.commit()
        log.info(f"{mint}\tverdict\t{'passed 8/8' if passed else 'rejected: ' + str(reason)}")
        bus.publish("audit", out)
        if passed and forward:
            await bus.auditor_to_analyst.put({"scanner": scan, "auditor": out})
        return out

    def rejected_tonight(self, mint: str) -> bool:
        return self.ctx.conn.execute(
            "SELECT 1 FROM audits WHERE mint=? AND pass=0 AND ts>=?", (mint, self.ctx.night_start())
        ).fetchone() is not None

    async def handle(self, scan: dict) -> dict | None:
        mint = scan["mint"]
        if self.rejected_tonight(mint):
            log.info(f"{mint}\tskip\talready rejected tonight")
            return None
        if not self.ctx.cfg()["auditor.enabled"]:
            # "Take apart before buying" switched off by the owner (asked twice in the app).
            log.info(f"{mint}\tauditor disabled\tpassed through unchecked")
            out = {"agent": "auditor", "mint": mint, "ts": self.ctx.now_iso(), "passed": True,
                   "layersChecked": 0, "layers": [], "holders": None, "reason": "auditor off"}
            await bus.auditor_to_analyst.put({"scanner": scan, "auditor": out})
            return out
        return await self.audit(scan)

    async def run(self) -> None:
        sem = asyncio.Semaphore(3)
        tasks: set[asyncio.Task] = set()

        async def one(scan):
            async with sem:
                try:
                    await self.handle(scan)
                except Exception as e:  # noqa: BLE001
                    log.info(f"{scan.get('mint')}\taudit_error\t{type(e).__name__}: {e}")

        while True:
            bus.beat("auditor")
            try:
                scan = await asyncio.wait_for(bus.scanner_to_auditor.get(), timeout=15)
            except asyncio.TimeoutError:
                continue
            while bus.is_paused("auditor"):
                bus.beat("auditor")
                await asyncio.sleep(2)
            t = asyncio.create_task(one(scan))
            tasks.add(t)
            t.add_done_callback(tasks.discard)


async def run(ctx: Ctx) -> None:
    await Auditor(ctx).run()
