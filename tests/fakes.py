"""Offline fakes for the outside sources (no network in tests)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import bus
import config
import db
from notifier import Notifier
from runtime import Ctx, iso
from sources import RateLimited, SourceError

MINT = "FakeMint1111111111111111111111111111111pump"
POOL = "FakePool11111111111111111111111111111111111"
CREATOR = "FakeCreator111111111111111111111111111111111"


class Clock:
    def __init__(self, start: datetime | None = None):
        self.t = start or datetime(2026, 11, 14, 3, 0, tzinfo=timezone.utc)  # 22:00 New York

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


class FakeDex:
    def __init__(self):
        self.views: dict[str, dict] = {}
        self.calls = 0
        self.raise_429 = False

    async def tokens(self, mints):
        self.calls += 1
        if self.raise_429:
            raise RateLimited("dex")
        return {m: dict(self.views[m]) for m in mints if m in self.views}


class FakeRpc:
    def __init__(self):
        self.mint_authority = None
        self.freeze_authority = None
        self.supply = 1_000_000_000_000
        self.creator_balance = 5_000_000_000      # 0.5%
        self.largest = [{"address": f"Acc{i}", "amount": str(a)} for i, a in
                        enumerate([300_000_000_000, 20_000_000_000, 15_000_000_000, 10_000_000_000,
                                   9_000_000_000, 8_000_000_000, 7_000_000_000, 6_000_000_000,
                                   5_000_000_000, 4_000_000_000, 3_000_000_000, 2_000_000_000])]
        self.owner_map = {"Acc0": POOL}
        self.program_accounts_list = [
            {"account": {"data": {"parsed": {"info": {"tokenAmount": {"amount": "100"}, "delegate": None}}}}}
            for _ in range(41)
        ]
        self.gpa_fail = False

    async def account_info(self, mint):
        return {"result": {"value": {"data": {"parsed": {"info": {
            "mintAuthority": self.mint_authority, "freezeAuthority": self.freeze_authority,
            "supply": str(self.supply), "decimals": 6}}}, "owner": "TokenzQd"}}}

    async def token_supply(self, mint):
        return self.supply, 6

    async def largest_accounts(self, mint):
        return self.largest

    async def owners(self, pubkeys):
        return {p: self.owner_map.get(p, "Wallet" + p) for p in pubkeys}

    async def owner_balance(self, owner, mint):
        return self.creator_balance

    async def program_accounts(self, mint):
        if self.gpa_fail:
            raise SourceError("gpa refused")
        return self.program_accounts_list


class FakeRug:
    def __init__(self):
        self.report_body = {
            "creator": CREATOR,
            "markets": [{"pubkey": POOL, "lp": {"lpLockedPct": 100, "lpCurrentSupply": 0,
                                                 "quoteUSD": 25000.0, "baseUSD": 25000.0}}],
        }
        self.graph_body = [{"net_id": "x", "nodes": [{"id": "W1", "holdings": 5}, {"id": "W2", "holdings": 3}],
                            "links": [{"source": "W1", "target": "W2"}]}]
        self.report_calls = 0
        self.graph_calls = 0

    async def report(self, mint):
        self.report_calls += 1
        return self.report_body

    async def graph(self, mint):
        self.graph_calls += 1
        return self.graph_body


class FakeJev:
    def __init__(self):
        self.answers = {"alive_6h": 0.9, "organic": 0.9, "flush": 0.5, "chase": 0.1}
        self.fail = 0           # next N calls raise SourceError
        self.raw = None         # override the whole response
        self.calls = []

    async def ask(self, state, questions, timeout):
        self.calls.append({"state": state, "questions": questions})
        if self.fail:
            self.fail -= 1
            raise SourceError("jev HTTP 500")
        if self.raw is not None:
            return self.raw
        return {"model": "jev-1.14.0",
                "answers": {k: {"type": "noul", "noul": v} for k, v in self.answers.items()},
                "usage": {"input_tokens": 300, "output_tokens": 20}}


class FakeSources:
    def __init__(self):
        self.dex = FakeDex()
        self.rpc = FakeRpc()
        self.rugcheck = FakeRug()
        self.jev = FakeJev()


def make_ctx(tmp_path, **settings) -> Ctx:
    path = tmp_path / "t.db"
    db.init_db(path)
    conn = db.connect(path)
    config.seed(conn)
    for k, v in settings.items():
        config.set_value(conn, k, v, who="test")
    bus.init()
    n = Notifier()
    n.retry_delay_s = 0
    ctx = Ctx(conn=conn, src=FakeSources(), notifier=n, clock=Clock(), fast=True)
    return ctx


def add_coin(ctx: Ctx, mint=MINT, name="Frog", migrated_min_ago=43, creator=CREATOR, creator_bal=5_000_000_000):
    mig = iso(ctx.now() - timedelta(minutes=migrated_min_ago))
    ctx.conn.execute(
        "INSERT OR REPLACE INTO coins(mint, name, symbol, pool, migrated_at, creator, creator_balance_at_migration)"
        " VALUES (?,?,?,?,?,?,?)", (mint, name, name.upper(), POOL, mig, creator, creator_bal))
    ctx.conn.commit()


def dex_view(price=0.001, liq=50000.0, **kw):
    v = {"pairAddress": POOL, "name": "Frog", "symbol": "FROG", "priceUsd": price, "liquidityUsd": liq,
         "marketCap": 900000, "volumeM5": 4000, "volumeH1": 30000, "volumeH24": 60000,
         "buysM5": 40, "sellsM5": 30, "buysH1": 200, "sellsH1": 150, "changeM5": -2.0,
         "changeH1": 20.0, "changeH24": 50.0, "pairCreatedAt": None}
    v.update(kw)
    return v


def run(coro):
    return asyncio.run(coro)
