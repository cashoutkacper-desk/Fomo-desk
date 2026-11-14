"""STEP 8 PUSH · Web Push with VAPID. Sends the same pushes to subscribed devices.

Plan A only: the app POSTs a subscription to /api/push (stored in db.push_subs).
Plan B has no endpoint to POST to, so push_subs stays empty and this sends nothing.
The VAPID private key is read from secrets/ at send time; never printed or logged.
Tapping the push opens `url`, which must be the exact links.coin_url string.
410 Gone (or 404) = that subscription is deleted.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

import config
import logutil

log = logutil.get("webpush")
VAPID_SUB = "mailto:desk@users.noreply.github.com"
TITLE = "Grok Bot · now"


def _vapid_key() -> str | None:
    p = config.SECRETS_DIR / "vapid_private.pem"
    return str(p) if p.is_file() else None


def payload(line: str, url: str, icon: str | None = None) -> dict[str, Any]:
    """header "Grok Bot · now", 1 line, the FOMO link from links.py (never rebuilt)."""
    if not url.startswith("https://") or "r=savipww" not in url:
        raise ValueError("push url must be the links.py URL with r=savipww")
    out = {"title": TITLE, "body": line, "url": url}
    if icon:
        out["icon"] = icon
    return out


def send_sync(conn, line: str, url: str, icon: str | None = None,
              sender: Callable[..., Any] | None = None) -> dict[str, int]:
    subs = conn.execute("SELECT endpoint, p256dh, auth FROM push_subs").fetchall()
    if not subs:
        return {"sent": 0, "deleted": 0, "failed": 0}
    key = _vapid_key()
    if key is None:
        log.info("web push skipped: no VAPID key")
        return {"sent": 0, "deleted": 0, "failed": len(subs)}
    if sender is None:
        from pywebpush import webpush as sender  # type: ignore
    data = json.dumps(payload(line, url, icon))
    sent = deleted = failed = 0
    for ep, p256dh, auth in subs:
        try:
            sender(subscription_info={"endpoint": ep, "keys": {"p256dh": p256dh, "auth": auth}},
                   data=data, vapid_private_key=key, vapid_claims={"sub": VAPID_SUB}, ttl=3600, timeout=10)
            sent += 1
        except Exception as e:  # pywebpush.WebPushException carries .response
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (404, 410):
                conn.execute("DELETE FROM push_subs WHERE endpoint=?", (ep,))
                deleted += 1
            else:
                failed += 1
                log.info(f"web push failed (status {status})")
    conn.commit()
    return {"sent": sent, "deleted": deleted, "failed": failed}


async def send_push(conn, line: str, url: str, icon: str | None = None, sender=None) -> dict[str, int]:
    return await asyncio.to_thread(send_sync, conn, line, url, icon, sender)
