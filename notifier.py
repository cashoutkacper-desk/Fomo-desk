"""Notifier (STEP 6): takes the URL from links.py and puts it on the button.

Channels:
- TelegramNotifier: the real channel. Plan B hosting has no web push, so Telegram is
  the only alert channel. Sends ONLY to settings.owner_chat_id (owner lock); every
  other chat is ignored. Token from secrets/TG_TOKEN (all whitespace stripped),
  never printed, never logged, never put in an exception message we log.
- Notifier (outbox): records to memory, logs/notifier.log and logs/outbox.jsonl.
  Used by tests and as the fallback when no token is set.

Link rules (STEP 6, enforced in check_links before anything is sent):
- every FOMO URL on a button is the exact string links.py returned and keeps r=savipww;
- no shortened / wrapped link anywhere (t.co, bit.ly, redirect pages, tracking proxies);
- a channel without buttons gets the full links.py URL as a line under the message
  (render_plain). A channel that can't carry the full URL is not allowed.
PAPER pushes start with "PAPER · ".
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any

import logutil
from config import LOG_DIR, read_secret
from db import utc_now
from links import FOMO_HOST, SHORTENERS, coin_url, is_valid_fomo_url

PAPER_PREFIX = "PAPER · "
TG_API = "https://api.telegram.org/bot{token}/{method}"
TG_MAX_TEXT = 4096
TG_MAX_CALLBACK = 64
_URL_RE = re.compile(r"https?://\S+")


class LinkRuleError(ValueError):
    """A message broke a STEP 6 link rule; it is refused, never sent."""


def fomo_button(text: str, mint: str, chain: str = "solana") -> dict[str, str]:
    return {"text": text, "url": coin_url(mint, chain)}


def callback_button(text: str, action: str, mint: str) -> dict[str, str]:
    return {"text": text, "callback": f"{action}:{mint}"}


def check_links(msg: dict[str, Any]) -> None:
    """Refuse any message that would carry a bad FOMO link."""
    urls = [b["url"] for b in msg.get("buttons", []) if b.get("url")]
    urls += _URL_RE.findall(msg.get("text") or "")
    for u in urls:
        low = u.lower()
        if any(f"//{s}" in low or f".{s}" in low for s in SHORTENERS):
            raise LinkRuleError("shortened/wrapped link")
        if FOMO_HOST in low and not is_valid_fomo_url(u):
            raise LinkRuleError("FOMO link not from links.py (missing r=savipww or wrapped)")


def render_plain(msg: dict[str, Any]) -> str:
    """For a channel without buttons: the full links.py URL as a line under the message."""
    lines = [msg["text"]]
    for b in msg.get("buttons", []):
        if b.get("url"):
            lines.append(f"{b['text']}: {b['url']}")
    return "\n".join(lines)


def telegram_markup(buttons: list[dict[str, str]]) -> dict | None:
    """Inline keyboard: URL buttons carry the exact links.py string. 1 row, unless a
    button carries "row": n (STEP 9 lists, e.g. "which coin?")."""
    if any("row" in b for b in buttons):
        rows: dict[int, list] = {}
        for b in buttons:
            m = telegram_markup([{k: v for k, v in b.items() if k != "row"}])
            if m:
                rows.setdefault(int(b.get("row", 0)), []).extend(m["inline_keyboard"][0])
        return {"inline_keyboard": [rows[k] for k in sorted(rows)]} if rows else None
    row = []
    for b in buttons:
        if b.get("url"):
            row.append({"text": b["text"], "url": b["url"]})
        elif b.get("callback"):
            data = b["callback"]
            if len(data.encode()) > TG_MAX_CALLBACK:
                raise LinkRuleError("callback data over 64 bytes")
            row.append({"text": b["text"], "callback_data": data})
    return {"inline_keyboard": [row]} if row else None


@dataclass
class Notifier:
    """Outbox channel (no network)."""

    channel: str = "outbox"
    sent: list[dict[str, Any]] = field(default_factory=list)
    fail_times: int = 0          # tests: make the next N sends fail
    retry_delay_s: float = 1.0
    supports_buttons: bool = True

    async def _send(self, msg: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    async def _deliver(self, msg: dict[str, Any]) -> dict[str, Any]:
        check_links(msg)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("notifier down (simulated)")
        if not self.supports_buttons:
            msg = {**msg, "text": render_plain(msg), "buttons": []}
        res = await self._send(msg)
        rec = {**msg, "channel": self.channel, "result": res}
        self.sent.append(rec)
        logutil.get("notifier").info(json.dumps(rec, ensure_ascii=False))
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_DIR / "outbox.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return res

    async def push(self, text: str, *, mode: str, mint: str | None = None,
                   buttons: list[dict[str, str]] | None = None, kind: str = "push",
                   retries: int = 3) -> bool:
        """Send 1 push. Retries `retries` times. Returns False if still down.
        A link-rule violation is never retried and never sent."""
        if mode == "PAPER" and not text.startswith(PAPER_PREFIX):
            text = PAPER_PREFIX + text
        msg = {"ts": utc_now(), "kind": kind, "mode": mode, "mint": mint, "text": text,
               "buttons": buttons or []}
        for attempt in range(retries + 1):
            try:
                await self._deliver(msg)
                return True
            except LinkRuleError as e:
                logutil.get("notifier").info(f"REFUSED push ({e}): {text[:80]}")
                return False
            except Exception as e:  # noqa: BLE001
                logutil.get("notifier").info(f"push attempt {attempt + 1} failed: {type(e).__name__}")
                if attempt < retries:
                    await asyncio.sleep(self.retry_delay_s)
        return False

    async def message(self, text: str, *, kind: str = "info",
                      buttons: list[dict[str, str]] | None = None) -> bool:
        """Plain message to the owner (warnings, ticket check, health)."""
        msg = {"ts": utc_now(), "kind": kind, "mode": None, "mint": None, "text": text,
               "buttons": buttons or []}
        try:
            await self._deliver(msg)
            return True
        except Exception as e:  # noqa: BLE001
            logutil.get("notifier").info(f"message failed: {type(e).__name__}")
            return False


async def push_reject(notifier: "Notifier", cfg: dict[str, Any], mint: str, name: str,
                      who: str, reason: str) -> bool:
    """STEP 10 push_rejects (default false): when true, every reject that writes an
    Activity "Rejected X" row is also pushed, with its FOMO link from links.py."""
    if not cfg.get("push_rejects"):
        return False
    return await notifier.push(f"Rejected {name} · {who} · {reason}", mode=cfg["mode"], mint=mint,
                               buttons=[fomo_button("Open on FOMO", mint)], kind="reject", retries=1)


class TelegramError(Exception):
    """Telegram API failure. The message never contains the token or URL."""


@dataclass
class TelegramNotifier(Notifier):
    """Telegram Bot API channel. Sends only to the owner chat."""

    channel: str = "telegram"
    owner_chat_id: int | None = None
    client: Any = None                     # httpx.AsyncClient
    token_getter: Any = None               # callable -> token; default reads secrets/TG_TOKEN

    def _token(self) -> str:
        tok = (self.token_getter or (lambda: read_secret("TG_TOKEN")))()
        if not tok:
            raise TelegramError("no TG_TOKEN")
        return "".join(tok.split())        # strip ALL whitespace

    async def _call(self, method: str, payload: dict, timeout: float = 20) -> dict:
        import httpx

        own = self.client is None
        client = self.client or httpx.AsyncClient()
        try:
            r = await client.post(TG_API.format(token=self._token(), method=method), json=payload, timeout=timeout)
            body = r.json()
        except TelegramError:
            raise
        except Exception as e:  # noqa: BLE001  (httpx errors embed the URL = the token)
            raise TelegramError(f"{method}: {type(e).__name__}") from None
        finally:
            if own:
                await client.aclose()
        if not body.get("ok"):
            raise TelegramError(f"{method}: HTTP {r.status_code} {body.get('description', '')[:120]}")
        return body

    async def _send(self, msg: dict[str, Any]) -> dict[str, Any]:
        if self.owner_chat_id is None:
            raise TelegramError("no owner_chat_id")
        text = msg["text"]
        if len(text) > TG_MAX_TEXT:
            raise TelegramError("text over 4096 chars")
        payload = {"chat_id": self.owner_chat_id, "text": text,
                   "link_preview_options": {"is_disabled": True}}
        markup = telegram_markup(msg.get("buttons", []))
        if markup:
            payload["reply_markup"] = markup
        body = await self._call("sendMessage", payload)
        res = body.get("result") or {}
        return {"ok": True, "message_id": res.get("message_id")}

    def is_owner(self, chat_id: Any) -> bool:
        """Owner lock: every update from another chat is ignored (STEP 9 polling uses this)."""
        try:
            return self.owner_chat_id is not None and int(chat_id) == int(self.owner_chat_id)
        except (TypeError, ValueError):
            return False


def build(cfg: dict[str, Any], client: Any = None) -> Notifier:
    """The desk's notifier: Telegram when settings.notifier = telegram and a token +
    owner_chat_id exist; else the outbox (and a log line says why)."""
    if cfg.get("notifier") == "telegram":
        owner = cfg.get("owner_chat_id")
        if read_secret("TG_TOKEN") and owner:
            return TelegramNotifier(owner_chat_id=int(owner), client=client)
        logutil.get("notifier").info("telegram selected but token/owner missing -> outbox")
    return Notifier()
