"""STEP 9 · Telegram commands, callbacks and the owner lock (offline: fake Bot API)."""

from __future__ import annotations

import time
from datetime import timedelta

import bus
import config
import telegram
from agents.auditor import Auditor
from links import SIGNUP_URL, coin_url, is_valid_fomo_url
from notifier import TelegramNotifier
from runtime import iso
from tests.fakes import MINT, POOL, add_coin, dex_view, make_ctx, run

OWNER = 6307862350
STRANGER = 111222333
FAKE_TOKEN = "123456:FAKE-token-for-tests"
MINT2 = "Frog2Mint111111111111111111111111111111pump"


class FakeTG(TelegramNotifier):
    def __init__(self):
        super().__init__(owner_chat_id=OWNER, token_getter=lambda: FAKE_TOKEN)
        self.calls: list[tuple[str, dict]] = []
        self.retry_delay_s = 0
        self.updates: list[dict] = []

    async def _call(self, method, payload, timeout=20):
        self.calls.append((method, payload))
        if method == "getUpdates":
            ups, self.updates = self.updates, []
            return {"ok": True, "result": ups}
        return {"ok": True, "result": {"message_id": len(self.calls)}}

    def sends(self):
        return [p for m, p in self.calls if m == "sendMessage"]


def setup(tmp_path, **kw):
    ctx = make_ctx(tmp_path, **kw)
    add_coin(ctx)
    tg = FakeTG()
    desk = telegram.Desk(ctx.conn, auditor=Auditor(ctx), dex=ctx.src.dex, clock=ctx.clock, log_dir=tmp_path)
    return ctx, tg, telegram.Bot(tg, desk), desk


_uid = [0]


def msg(text, chat=OWNER, age_s=0):
    _uid[0] += 1
    return {"update_id": _uid[0], "message": {"chat": {"id": chat}, "date": int(time.time()) - age_s, "text": text}}


def say(bot, tg, text, chat=OWNER):
    tg.calls.clear()
    tg.updates = [msg(text, chat)]
    run(bot.poll_once())
    return tg.sends()


def buttons(p):
    return [b for row in (p.get("reply_markup") or {}).get("inline_keyboard", []) for b in row]


def test_non_owner_is_ignored(tmp_path):
    ctx, tg, bot, _ = setup(tmp_path)
    assert say(bot, tg, "status", chat=STRANGER) == []
    tg.updates = [{"update_id": 999, "callback_query": {"id": "q", "data": f"bought:{MINT}",
                                                         "message": {"chat": {"id": STRANGER}}}}]
    run(bot.poll_once())
    assert [m for m, _ in tg.calls if m != "getUpdates"] == []
    assert bus.executor_callbacks.empty()


def test_replies_only_to_owner_chat_and_unknown_text_shows_list(tmp_path):
    ctx, tg, bot, _ = setup(tmp_path)
    out = say(bot, tg, "hello desk")
    assert len(out) == 1 and out[0]["chat_id"] == OWNER and out[0]["text"] == telegram.HELP
    assert say(bot, tg, "/start")[0]["text"] == telegram.HELP


def test_set_stop_with_alias_and_change_log(tmp_path):
    ctx, tg, bot, _ = setup(tmp_path)
    assert say(bot, tg, "set stop to -8%")[0]["text"] == "stop -6% -> -8%"
    assert config.load(ctx.conn)["risk.stop_pct"] == 8.0
    row = ctx.conn.execute("SELECT key, old, new, who FROM change_log ORDER BY rowid DESC LIMIT 1").fetchone()
    assert tuple(row) == ("risk.stop_pct", "6.0", "8.0", "telegram")
    assert say(bot, tg, "/set min_score 75")[0]["text"] == "min_score 70 -> 75"


def test_set_out_of_range_and_mode_refused(tmp_path):
    ctx, tg, bot, _ = setup(tmp_path)
    t = say(bot, tg, "set stop to -95%")[0]["text"]
    assert t.startswith("Not changed:") and "between" in t
    assert config.load(ctx.conn)["risk.stop_pct"] == 6.0
    assert say(bot, tg, "set mode LIVE")[0]["text"] == "Mode switches with: paper | live"
    assert config.load(ctx.conn)["mode"] == "PAPER"


def test_stack_sets_live_stack(tmp_path):
    ctx, tg, bot, _ = setup(tmp_path)
    t = say(bot, tg, "stack 1240")[0]["text"]
    assert t.startswith("stack $0.00 -> $1,240.00")
    assert config.load(ctx.conn)["live_stack"] == 1240.0


def test_pause_resume_and_risk_lock(tmp_path):
    ctx, tg, bot, _ = setup(tmp_path)
    assert say(bot, tg, "pause Scanner")[0]["text"] == "Scanner running -> paused"
    assert bus.is_paused("scanner")
    assert say(bot, tg, "resume scanner")[0]["text"] == "Scanner paused -> running"
    ctx.conn.execute("INSERT INTO positions(mint, opened_at, entry_price, size_usd, peak_price, stop_price,"
                     " trail_active, mode) VALUES (?,?,?,?,?,?,?,?)", (MINT, iso(ctx.now()), 1.0, 47.5, 1.0, 0.94, 0, "PAPER"))
    ctx.conn.commit()
    assert "Risk can't be paused while a position is open" in say(bot, tg, "pause Risk")[0]["text"]
    assert not bus.is_paused("risk")
    keys = [r[0] for r in ctx.conn.execute("SELECT key FROM change_log WHERE who='telegram'")]
    assert keys == ["agent.scanner", "agent.scanner"]


def test_live_needs_paper_night_then_typed_LIVE(tmp_path, monkeypatch):
    ctx, tg, bot, desk = setup(tmp_path)
    assert "full PAPER night" in say(bot, tg, "live")[0]["text"]
    monkeypatch.setattr(config, "full_paper_night", lambda conn, now=None, max_gap_min=10: True)
    assert say(bot, tg, "live")[0]["text"].startswith("Type LIVE")
    assert say(bot, tg, "status")[0]["text"].startswith("PAPER")      # anything else cancels
    assert say(bot, tg, "LIVE")[0]["text"].startswith("Type LIVE")     # not pending -> asks again
    say(bot, tg, "live")
    ctx.clock.advance(seconds=121)
    assert say(bot, tg, "LIVE")[0]["text"].startswith("Too late")
    assert config.load(ctx.conn)["mode"] == "PAPER"
    say(bot, tg, "live")
    assert say(bot, tg, "LIVE")[0]["text"] == "mode PAPER -> LIVE"
    assert say(bot, tg, "paper")[0]["text"] == "mode LIVE -> PAPER"


def test_buy_pushed_tonight_resends_push_with_button(tmp_path):
    ctx, tg, bot, _ = setup(tmp_path)
    ctx.conn.execute("INSERT INTO verdicts(mint, ts, score, pass) VALUES (?,?,?,1)", (MINT, iso(ctx.now()), 82))
    ctx.conn.execute("INSERT INTO signals(mint, ts, price, size_usd, stack_pct, link, mode, status) VALUES (?,?,?,?,?,?,?,?)",
                     (MINT, iso(ctx.now()), 0.001, 47.5, 23.75, coin_url(MINT), "PAPER", "bought"))
    ctx.conn.commit()
    out = say(bot, tg, "buy FROG")
    assert out[0]["text"] == ("PAPER · Buy Frog · $47.50 · 23.75% of stack\n"
                              "Auditor 8/8 · Analyst 82 · stop -6% · trail 12%")
    assert buttons(out[0]) == [{"text": "Buy on FOMO", "url": coin_url(MINT, "solana")}]


def test_buy_never_pushed_runs_auditor_now_not_on_desk(tmp_path):
    ctx, tg, bot, _ = setup(tmp_path)
    ctx.src.dex.views[MINT] = dex_view()
    out = say(bot, tg, f"buy {MINT}")
    t = out[0]["text"]
    assert t.startswith("Frog · Auditor now · Cleared · 8/8 parts")
    assert "✅ 1 Mint authority · Revoked" in t and "Not sized, not on the desk. No order is placed." in t
    assert buttons(out[0]) == [{"text": "Buy on FOMO", "url": coin_url(MINT, "solana")}]
    assert bus.auditor_to_analyst.empty()                       # not sent on to the desk
    assert ctx.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0
    assert ctx.conn.execute("SELECT COUNT(*) FROM audits WHERE mint=?", (MINT,)).fetchone()[0] == 8


def test_unknown_name_asks_mint_and_two_matches_ask_which(tmp_path):
    ctx, tg, bot, _ = setup(tmp_path)
    assert say(bot, tg, "buy PEPE")[0]["text"] == "I don't know PEPE. Send me its mint."
    add_coin(ctx, mint=MINT2, name="Frog")
    out = say(bot, tg, "why frog")[0]
    assert out["text"].startswith("frog matches 2 coins. Which one?")
    urls = [b["url"] for b in buttons(out)]
    assert sorted(urls) == sorted([coin_url(MINT), coin_url(MINT2)])
    assert len(out["reply_markup"]["inline_keyboard"]) == 2


def test_sell_and_status_carry_sell_on_fomo(tmp_path):
    ctx, tg, bot, _ = setup(tmp_path)
    assert say(bot, tg, "sell FROG")[0]["text"] == "No open Frog position."
    ctx.conn.execute("INSERT INTO positions(mint, opened_at, entry_price, size_usd, peak_price, stop_price,"
                     " trail_active, mode) VALUES (?,?,?,?,?,?,?,?)", (MINT, iso(ctx.now()), 0.001, 47.5, 0.001, 0.00094, 0, "PAPER"))
    ctx.conn.execute("INSERT INTO prices(writer, mint, ts, price, volume_m5) VALUES (?,?,?,?,?)", ("risk", MINT, iso(ctx.now()), 0.0011, 1))
    ctx.conn.commit()
    s = say(bot, tg, "sell FROG")[0]
    assert s["text"] == "PAPER · Sell Frog · $47.50 in · +10.0% since entry"
    assert buttons(s) == [{"text": "Sell on FOMO", "url": coin_url(MINT)}]
    st = say(bot, tg, "status")
    assert st[0]["text"] == "PAPER · balance $200.00 · 1 open position"
    assert buttons(st[1]) == [{"text": "Sell on FOMO", "url": coin_url(MINT)}]


def test_why_names_agent_layer_value_time(tmp_path):
    ctx, tg, bot, _ = setup(tmp_path)
    ctx.conn.execute("INSERT INTO audits(mint, ts, layer, name, value_text, pass, source) VALUES (?,?,?,?,?,?,?)",
                     (MINT, "2026-11-14T03:00:00.000Z", 3, "Dev wallet", "dev holds 31%", 0, "rpc"))
    ctx.conn.commit()
    out = say(bot, tg, "why FROG")[0]
    assert out["text"] == "Frog: stopped by Auditor · layer 3 Dev wallet · dev holds 31% · 10:00 PM"
    assert buttons(out)[0]["url"] == coin_url(MINT)
    ctx.conn.execute("INSERT INTO verdicts(mint, ts, score, pass, reason) VALUES (?,?,?,0,?)",
                     (MINT, "2026-11-14T03:05:00.000Z", 40, "chase 0.6"))
    ctx.conn.commit()
    assert say(bot, tg, "why FROG")[0]["text"] == "Frog: stopped by Analyst · rule chase 0.6 · score 40 · 10:05 PM"


def test_invite_and_report(tmp_path):
    ctx, tg, bot, _ = setup(tmp_path)
    assert SIGNUP_URL in say(bot, tg, "invite")[0]["text"]
    assert say(bot, tg, "report")[0]["text"] == "No morning Page yet."


def test_callbacks_go_to_agents(tmp_path):
    ctx, tg, bot, _ = setup(tmp_path)

    async def go():
        tg.updates = [{"update_id": 501, "callback_query": {"id": "a", "data": f"bought:{MINT}", "message": {"chat": {"id": OWNER}}}},
                      {"update_id": 502, "callback_query": {"id": "b", "data": f"sold:{MINT}", "message": {"chat": {"id": OWNER}}}}]
        await bot.poll_once()
        return bus.executor_callbacks.get_nowait(), bus.risk_callbacks.get_nowait()
    assert run(go()) == (("bought", MINT), ("sold", MINT))
    assert [m for m, _ in tg.calls].count("answerCallbackQuery") == 2


def test_old_backlog_is_skipped(tmp_path):
    ctx, tg, bot, _ = setup(tmp_path)
    tg.updates = [msg("set stop to -9%", age_s=3600)]
    run(bot.poll_once())
    assert tg.sends() == [] and config.load(ctx.conn)["risk.stop_pct"] == 6.0


def test_every_button_is_a_links_py_url_and_token_never_logged(tmp_path):
    ctx, tg, bot, _ = setup(tmp_path)
    ctx.src.dex.views[MINT] = dex_view()
    for t in ("buy FROG", "sell FROG", "why FROG", "status", "invite", "nonsense"):
        for p in say(bot, tg, t):
            for b in buttons(p):
                if "url" in b:
                    assert is_valid_fomo_url(b["url"]) and "r=savipww" in b["url"]
    for f in tmp_path.rglob("*"):
        if f.is_file() and f.suffix in (".log", ".jsonl", ""):
            assert FAKE_TOKEN not in f.read_text(errors="replace")
    assert FAKE_TOKEN not in (config.LOG_DIR / "telegram.log").read_text() if (config.LOG_DIR / "telegram.log").exists() else True


def test_first_start_sets_owner_when_none(tmp_path):
    ctx, tg, bot, _ = setup(tmp_path)
    tg.owner_chat_id = None
    tg.updates = [msg("/start", chat=424242)]
    run(bot.poll_once())
    assert tg.owner_chat_id == 424242
    assert config.load(ctx.conn).get("owner_chat_id") in ("424242", 424242)
    assert say(bot, tg, "status", chat=OWNER) == []           # now everyone else is ignored
