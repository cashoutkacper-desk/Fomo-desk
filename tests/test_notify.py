"""STEP 6 · notifier.py puts the links.py URL on the button (Telegram, owner only)."""

from __future__ import annotations

import json

import pytest

from links import coin_url
from notifier import (LinkRuleError, Notifier, TelegramError, TelegramNotifier, callback_button,
                      check_links, fomo_button, render_plain, telegram_markup)
from tests.fakes import run

MINT = "4Ye6Y6Vaic8cyPdfW2f9PcLhXkysBtSmV84G8X7spump"
OWNER = 6307862350
FAKE_TOKEN = "123456:FAKE-token-for-tests-only"


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._b = status, body

    def json(self):
        return self._b


class FakeClient:
    def __init__(self, body=None, exc=None):
        self.calls, self.body, self.exc = [], body, exc

    async def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json})
        if self.exc:
            raise self.exc(f"boom while calling {url}")
        return FakeResp(200, self.body or {"ok": True, "result": {"message_id": 42}})


def _tg(client):
    n = TelegramNotifier(owner_chat_id=OWNER, client=client, token_getter=lambda: f"  {FAKE_TOKEN}\n")
    n.retry_delay_s = 0
    return n


def test_button_url_is_exact_links_string():
    b = fomo_button("Buy on FOMO", MINT)
    assert b["url"] == coin_url(MINT, "solana")
    m = telegram_markup([b, callback_button("Bought", "bought", MINT)])
    assert m["inline_keyboard"][0][0] == {"text": "Buy on FOMO", "url": coin_url(MINT, "solana")}
    assert m["inline_keyboard"][0][1] == {"text": "Bought", "callback_data": f"bought:{MINT}"}


def test_telegram_sends_paper_push_to_owner_only_with_exact_url():
    c = FakeClient()
    n = _tg(c)
    ok = run(n.push("Buy Frog · $47.50", mode="PAPER", mint=MINT, buttons=[fomo_button("Buy on FOMO", MINT)]))
    assert ok and len(c.calls) == 1
    p = c.calls[0]["json"]
    assert p["chat_id"] == OWNER and p["text"].startswith("PAPER · Buy Frog")
    assert p["reply_markup"]["inline_keyboard"][0][0]["url"] == coin_url(MINT, "solana")
    assert "r=savipww" in p["reply_markup"]["inline_keyboard"][0][0]["url"]
    assert c.calls[0]["url"].endswith("/sendMessage") and " " not in c.calls[0]["url"]  # whitespace stripped
    assert n.sent[-1]["result"] == {"ok": True, "message_id": 42}


def test_token_never_in_logs_or_errors(tmp_path):
    import os
    c = FakeClient(exc=ConnectionError)
    n = _tg(c)
    assert run(n.push("x", mode="PAPER", mint=MINT, buttons=[fomo_button("Sell on FOMO", MINT)], retries=1)) is False
    with pytest.raises(TelegramError) as e:
        run(n._call("sendMessage", {}))
    assert FAKE_TOKEN not in str(e.value)
    log_dir = os.environ["DESK_LOG_DIR"]
    for f in os.listdir(log_dir):
        assert FAKE_TOKEN not in open(os.path.join(log_dir, f), errors="ignore").read()


def test_wrapped_or_stripped_fomo_link_refused():
    bad = [{"text": "Buy", "url": "https://bit.ly/3xyz"}]
    with pytest.raises(LinkRuleError):
        check_links({"text": "x", "buttons": bad})
    with pytest.raises(LinkRuleError):
        check_links({"text": "x", "buttons": [{"text": "Buy", "url":
                     f"https://fomo.family/coin?address={MINT}&chainId=1399811149"}]})
    with pytest.raises(LinkRuleError):
        check_links({"text": "go https://t.co/abc", "buttons": []})
    c = FakeClient()
    n = _tg(c)
    assert run(n.push("x", mode="PAPER", buttons=bad)) is False and c.calls == []   # never sent


def test_channel_without_buttons_gets_full_url_line():
    msg = {"text": "PAPER · Sold Frog · +$5.00 (+10%)", "buttons": [fomo_button("Sell on FOMO", MINT),
                                                                   callback_button("Sold", "sold", MINT)]}
    plain = render_plain(msg)
    assert plain.splitlines()[-1] == f"Sell on FOMO: {coin_url(MINT, 'solana')}"
    n = Notifier(supports_buttons=False)
    run(n.push("Sold Frog", mode="PAPER", mint=MINT, buttons=msg["buttons"]))
    assert coin_url(MINT, "solana") in n.sent[-1]["text"] and n.sent[-1]["buttons"] == []


def test_owner_lock():
    n = TelegramNotifier(owner_chat_id=OWNER)
    assert n.is_owner(OWNER) and n.is_owner(str(OWNER))
    assert not n.is_owner(111) and not n.is_owner(None)


def test_no_owner_means_no_send():
    c = FakeClient()
    n = TelegramNotifier(owner_chat_id=None, client=c, token_getter=lambda: FAKE_TOKEN)
    n.retry_delay_s = 0
    assert run(n.push("x", mode="PAPER", retries=0)) is False and c.calls == []


def test_telegram_api_not_ok_is_failure():
    c = FakeClient(body={"ok": False, "description": "Bad Request: chat not found"})
    n = _tg(c)
    assert run(n.push("x", mode="PAPER", retries=0)) is False


def test_build_picks_telegram_when_configured(monkeypatch):
    import notifier
    monkeypatch.setattr(notifier, "read_secret", lambda name: FAKE_TOKEN)
    n = notifier.build({"notifier": "telegram", "owner_chat_id": str(OWNER)})
    assert isinstance(n, TelegramNotifier) and n.owner_chat_id == OWNER
    monkeypatch.setattr(notifier, "read_secret", lambda name: None)
    assert not isinstance(notifier.build({"notifier": "telegram", "owner_chat_id": str(OWNER)}),
                          TelegramNotifier)
