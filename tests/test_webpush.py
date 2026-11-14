import sqlite3

import pytest

import webpush
from links import coin_url

MINT = "4tXXXy5LysuU8SHhdvoHBj9TYhXC1z2B5eS91EoXpump"


def _db():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE push_subs (endpoint TEXT PRIMARY KEY, p256dh TEXT, auth TEXT, created_at TEXT)")
    c.executemany("INSERT INTO push_subs VALUES (?,?,?,?)",
                  [("https://push.example/a", "k1", "a1", "t"), ("https://push.example/gone", "k2", "a2", "t")])
    return c


class Resp:
    def __init__(self, code):
        self.status_code = code


class Gone(Exception):
    def __init__(self, code):
        super().__init__("push failed")
        self.response = Resp(code)


def test_payload_uses_links_url_exactly():
    u = coin_url(MINT)
    p = webpush.payload("Sold CARD · +$4.10 (+8.2%)", u)
    assert p == {"title": "Grok Bot · now", "body": "Sold CARD · +$4.10 (+8.2%)", "url": u}


@pytest.mark.parametrize("bad", ["https://bit.ly/x", "http://x/coin?r=savipww", "https://x/coin?address=1"])
def test_payload_refuses_other_urls(bad):
    with pytest.raises(ValueError):
        webpush.payload("x", bad)


def test_410_gone_deletes_that_subscription(monkeypatch):
    monkeypatch.setattr(webpush, "_vapid_key", lambda: "/nonexistent.pem")
    calls = []

    def sender(subscription_info, data, **kw):
        calls.append((subscription_info["endpoint"], data))
        if subscription_info["endpoint"].endswith("gone"):
            raise Gone(410)

    c = _db()
    r = webpush.send_sync(c, "CARD hit the -6% stop · -$1.20", coin_url(MINT), sender=sender)
    assert r == {"sent": 1, "deleted": 1, "failed": 0}
    assert [e for (e,) in c.execute("SELECT endpoint FROM push_subs")] == ["https://push.example/a"]
    assert coin_url(MINT) in calls[0][1]


def test_other_errors_keep_subscription(monkeypatch):
    monkeypatch.setattr(webpush, "_vapid_key", lambda: "/nonexistent.pem")

    def sender(**kw):
        raise Gone(500)

    c = _db()
    assert webpush.send_sync(c, "x", coin_url(MINT), sender=sender) == {"sent": 0, "deleted": 0, "failed": 2}
    assert c.execute("SELECT COUNT(*) FROM push_subs").fetchone()[0] == 2


def test_no_subscriptions_sends_nothing():
    c = _db()
    c.execute("DELETE FROM push_subs")
    assert webpush.send_sync(c, "x", coin_url(MINT), sender=lambda **k: 1 / 0) == {"sent": 0, "deleted": 0, "failed": 0}
