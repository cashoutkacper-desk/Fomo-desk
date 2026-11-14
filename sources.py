"""Clients for the outside sources the agents read (no keys except Jev).

DexScreener, Solana RPC (settings.rpc_url), RugCheck, Jev (TypeSafe).
Raw keys are read through fields.py. Nothing here places orders.
Secrets: the Jev key is passed in by the caller and only ever put in the
Authorization header; it is never logged.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any

import httpx

import fields

DEX_URL = "https://api.dexscreener.com/tokens/v1/solana/{mints}"
RUGCHECK_REPORT = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
RUGCHECK_GRAPH = "https://api.rugcheck.xyz/v1/tokens/{mint}/insiders/graph"
JEV_URL = "https://api.typesafe.ai/v1/systemone"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"   # pumpfun bonding curve program
DEX_SRC = "dexscreener_pair.json"
RC_SRC = "rugcheck_report.json"
RPC_SRC = "rpc_getaccountinfo.json"


class RateLimited(Exception):
    """HTTP 429 from a source."""


class SourceError(Exception):
    pass


# ------------------------------------------------------------------ base58 / PDA
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + _B58.index(c)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + raw


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    pad = len(b) - len(b.lstrip(b"\x00"))
    return "1" * pad + out


_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def _on_curve(b: bytes) -> bool:
    """True if 32 bytes decode to a valid ed25519 point."""
    y = int.from_bytes(b, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= _P:
        return False
    y2 = y * y % _P
    x2 = (y2 - 1) * pow(_D * y2 + 1, _P - 2, _P) % _P
    if x2 == 0:
        return sign == 0
    return pow(x2, (_P - 1) // 2, _P) == 1


def find_program_address(seeds: list[bytes], program_id: str) -> str:
    prog = b58decode(program_id)
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([bump]) + prog + b"ProgramDerivedAddress").digest()
        if not _on_curve(h):
            return b58encode(h)
    raise ValueError("no PDA")


def bonding_curve_address(mint: str) -> str:
    return find_program_address([b"bonding-curve", b58decode(mint)], PUMP_PROGRAM)


# ------------------------------------------------------------------ DexScreener

def pick_pumpswap_pair(pairs: list[dict], mint: str) -> dict | None:
    """The PumpSwap pair (dexId pumpswap) of this mint; most liquidity wins."""
    best, best_liq = None, -1.0
    for p in pairs or []:
        if fields.get(p, "dex_id", DEX_SRC, element=True) != "pumpswap":
            continue
        if fields.get(p, "mint", DEX_SRC, element=True) != mint:
            continue
        liq = fields.get(p, "liquidity_usd", DEX_SRC, element=True) or 0
        if liq > best_liq:
            best, best_liq = p, liq
    return best


def dex_view(pair: dict) -> dict[str, Any]:
    """Flatten the fields the agents need from 1 DexScreener pair."""
    g = lambda f: fields.get(pair, f, DEX_SRC, element=True)  # noqa: E731
    price = g("price_usd")
    try:
        price = float(price) if price is not None else None
    except (TypeError, ValueError):
        price = None
    return {
        "pairAddress": g("pool_address"),
        "name": g("name"),
        "symbol": g("symbol"),
        "priceUsd": price,
        "liquidityUsd": g("liquidity_usd"),
        "marketCap": g("mcap"),
        "volumeM5": g("volume_m5"),
        "volumeH1": g("volume_h1"),
        "volumeH24": g("volume_h24"),
        "buysM5": g("txns_m5_buys"),
        "sellsM5": g("txns_m5_sells"),
        "buysH1": g("txns_h1_buys"),
        "sellsH1": g("txns_h1_sells"),
        "changeM5": g("price_change_m5"),
        "changeH1": g("price_change_h1"),
        "changeH24": g("price_change_h24"),
        "pairCreatedAt": g("pair_created"),
    }


class Dex:
    """Batched DexScreener reads with a per-minute request budget."""

    def __init__(self, client: httpx.AsyncClient, max_rpm: int = 250):
        self.client = client
        self.max_rpm = max_rpm
        self._stamps: list[float] = []
        self.requests = 0

    async def _budget(self) -> None:
        while True:
            now = time.monotonic()
            self._stamps = [t for t in self._stamps if now - t < 60]
            if len(self._stamps) < self.max_rpm:
                self._stamps.append(now)
                return
            await asyncio.sleep(60 - (now - self._stamps[0]) + 0.05)

    async def tokens(self, mints: list[str]) -> dict[str, dict]:
        """{mint: dex_view(pumpswap pair)} for every mint DexScreener knows."""
        out: dict[str, dict] = {}
        for i in range(0, len(mints), 30):
            chunk = mints[i : i + 30]
            await self._budget()
            self.requests += 1
            r = await self.client.get(DEX_URL.format(mints=",".join(chunk)), timeout=15)
            if r.status_code == 429:
                raise RateLimited("dexscreener")
            if r.status_code != 200:
                raise SourceError(f"dexscreener HTTP {r.status_code}")
            pairs = r.json() or []
            for m in chunk:
                p = pick_pumpswap_pair(pairs, m)
                if p is not None:
                    out[m] = dex_view(p)
        return out


# ------------------------------------------------------------------ Solana RPC

class Rpc:
    def __init__(self, client: httpx.AsyncClient, url_getter):
        self.client = client
        self.url_getter = url_getter  # callable -> settings.rpc_url (live)
        self.calls = 0

    async def call(self, method: str, params: list, timeout: float = 20) -> Any:
        self.calls += 1
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        r = await self.client.post(self.url_getter(), json=body, timeout=timeout)
        if r.status_code == 429:
            raise RateLimited(f"rpc {method}")
        if r.status_code != 200:
            raise SourceError(f"rpc {method} HTTP {r.status_code}")
        j = r.json()
        if "error" in j:
            raise SourceError(f"rpc {method} error {j['error'].get('code')}")
        return j.get("result")

    async def account_info(self, pubkey: str) -> dict | None:
        """getAccountInfo jsonParsed, wrapped like the raw sample ({'result': ...})."""
        res = await self.call("getAccountInfo", [pubkey, {"encoding": "jsonParsed"}])
        return {"result": res}

    async def token_supply(self, mint: str) -> tuple[int, int]:
        res = await self.call("getTokenSupply", [mint])
        v = res["value"]
        return int(v["amount"]), int(v["decimals"])

    async def largest_accounts(self, mint: str) -> list[dict]:
        res = await self.call("getTokenLargestAccounts", [mint])
        return res["value"]

    async def owners(self, pubkeys: list[str]) -> dict[str, str | None]:
        """Token account -> owner wallet (getMultipleAccounts jsonParsed)."""
        out: dict[str, str | None] = {}
        for i in range(0, len(pubkeys), 100):
            chunk = pubkeys[i : i + 100]
            res = await self.call("getMultipleAccounts", [chunk, {"encoding": "jsonParsed"}])
            for pk, acc in zip(chunk, res["value"]):
                owner = None
                try:
                    owner = acc["data"]["parsed"]["info"]["owner"]
                except (TypeError, KeyError):
                    pass
                out[pk] = owner
        return out

    async def owner_balance(self, owner: str, mint: str) -> int:
        """Raw token amount `owner` holds of `mint` (all its token accounts)."""
        res = await self.call(
            "getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed"}]
        )
        total = 0
        for acc in res.get("value", []):
            try:
                total += int(acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
            except (KeyError, TypeError, ValueError):
                pass
        return total

    async def program_accounts(self, mint: str) -> list[dict]:
        """Every token account of `mint` on Token AND Token-2022 (jsonParsed)."""
        out: list[dict] = []
        specs = (
            (TOKEN_PROGRAM, [{"dataSize": 165}, {"memcmp": {"offset": 0, "bytes": mint}}]),
            (TOKEN_2022_PROGRAM, [{"memcmp": {"offset": 0, "bytes": mint}}]),
        )
        for prog, filters in specs:
            res = await self.call(
                "getProgramAccounts",
                [prog, {"encoding": "jsonParsed", "filters": filters}],
                timeout=40,
            )
            out.extend(res or [])
        return out


def balances_from_program_accounts(accounts: list[dict]) -> list[tuple[str, str | None, int]]:
    """(token account, owner, raw amount) from getProgramAccounts jsonParsed."""
    out = []
    for acc in accounts:
        try:
            info = acc["account"]["data"]["parsed"]["info"]
            out.append((acc.get("pubkey"), info.get("owner"), int(info["tokenAmount"]["amount"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


async def top10_pct(rpc: Rpc, mint: str, exclude_owners: set[str], supply_raw: int,
                    log=None) -> float:
    """Top 10 holder accounts' share of supply, accounts owned by the pool / curve dropped.

    Primary: getTokenLargestAccounts (+ getMultipleAccounts for owners), as the spec says.
    The public mainnet-beta RPC answers that method with 429 "Too many requests for a
    specific RPC call" from this box, so on 429/error the same numbers are computed from
    getProgramAccounts (Token + Token-2022, filtered by mint), which that RPC serves.
    """
    try:
        largest = await rpc.largest_accounts(mint)
        owners = await rpc.owners([a["address"] for a in largest])
        rows = [(a["address"], owners.get(a["address"]), int(a["amount"])) for a in largest]
        via = "largest"
    except (RateLimited, SourceError) as e:
        rows = balances_from_program_accounts(await rpc.program_accounts(mint))
        via = f"gpa ({type(e).__name__})"
    kept = sorted((amt for addr, owner, amt in rows
                   if owner not in exclude_owners and addr not in exclude_owners), reverse=True)
    if log is not None:
        log.info(f"{mint}	top10 via {via}")
    return sum(kept[:10]) / supply_raw * 100 if supply_raw else 0.0


# ------------------------------------------------------------------ RugCheck

class RugCheck:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def _get(self, url: str) -> Any:
        r = await self.client.get(url, timeout=20)
        if r.status_code == 429:
            raise RateLimited("rugcheck")
        if r.status_code != 200:
            raise SourceError(f"rugcheck HTTP {r.status_code}")
        return r.json()

    async def report(self, mint: str) -> dict:
        return await self._get(RUGCHECK_REPORT.format(mint=mint))

    async def graph(self, mint: str) -> Any:
        return await self._get(RUGCHECK_GRAPH.format(mint=mint))


# ------------------------------------------------------------------ Jev

class Jev:
    def __init__(self, client: httpx.AsyncClient, key_getter):
        self.client = client
        self.key_getter = key_getter  # callable -> key or None; never logged

    async def ask(self, state: str, questions: dict, timeout: float) -> dict:
        key = self.key_getter()
        if not key:
            raise SourceError("no Jev key")
        body = {"model": "jev-latest", "state": state, "questions": questions}
        r = await self.client.post(
            JEV_URL, json=body, headers={"Authorization": f"Bearer {key}"}, timeout=timeout
        )
        if r.status_code == 429:
            raise RateLimited("jev")
        if r.status_code != 200:
            raise SourceError(f"jev HTTP {r.status_code}")
        return r.json()


class Sources:
    """Everything the agents read from outside, in one place (tests swap fakes in)."""

    def __init__(self, client: httpx.AsyncClient, rpc_url_getter, jev_key_getter, max_rpm=250):
        self.client = client
        self.dex = Dex(client, max_rpm)
        self.rpc = Rpc(client, rpc_url_getter)
        self.rugcheck = RugCheck(client)
        self.jev = Jev(client, jev_key_getter)
