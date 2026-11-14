"""Web Push with VAPID. Sends the same pushes to subscribed devices. STEP 8."""

from __future__ import annotations


async def send_push(title: str, body: str, payload: dict | None = None) -> None:
    raise NotImplementedError("webpush.py is implemented in STEP 8")
