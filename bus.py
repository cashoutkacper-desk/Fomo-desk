"""In-process bus: scanner -> auditor -> analyst -> executor -> risk.

Agents talk ONLY through these queues, the pubsub below, and the db.

- Queues carry one JSON-able dict per coin (the previous agent's OUTPUT plus
  the context the next agent's INPUT names).
- pubsub: SSE-style events for the app (scan, audit_layer, verdict, signal,
  fill, exit, balance). api.py (STEP 7) subscribes; tests subscribe too.
- callbacks: [Bought] / [Skip] / [Sold] button taps (STEP 9 feeds them).
- beat(): liveness for main.py's heartbeat writer.
- paused: agents skip work while paused (Risk can't be paused with an open position).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

AGENTS = ("scanner", "auditor", "analyst", "executor", "risk")

scanner_to_auditor: asyncio.Queue | None = None
auditor_to_analyst: asyncio.Queue | None = None
analyst_to_executor: asyncio.Queue | None = None
executor_to_risk: asyncio.Queue | None = None
executor_callbacks: asyncio.Queue | None = None   # ("bought"|"skip", mint)
risk_callbacks: asyncio.Queue | None = None       # ("sold", mint)

_subscribers: list[asyncio.Queue] = []
_beats: dict[str, float] = {}
paused: set[str] = set()
history: list[tuple[str, dict]] = []   # last events, for tests / debugging
HISTORY_MAX = 500


def init() -> None:
    """Create fresh queues bound to the running loop."""
    global scanner_to_auditor, auditor_to_analyst, analyst_to_executor, executor_to_risk
    global executor_callbacks, risk_callbacks
    scanner_to_auditor = asyncio.Queue()
    auditor_to_analyst = asyncio.Queue()
    analyst_to_executor = asyncio.Queue()
    executor_to_risk = asyncio.Queue()
    executor_callbacks = asyncio.Queue()
    risk_callbacks = asyncio.Queue()
    _subscribers.clear()
    _beats.clear()
    paused.clear()
    history.clear()


def subscribe(maxsize: int = 1000) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
    _subscribers.append(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    if q in _subscribers:
        _subscribers.remove(q)


def publish(event: str, data: dict[str, Any]) -> None:
    history.append((event, data))
    if len(history) > HISTORY_MAX:
        del history[: len(history) - HISTORY_MAX]
    for q in list(_subscribers):
        try:
            q.put_nowait((event, data))
        except asyncio.QueueFull:
            pass  # slow subscriber drops events, agents never block on the app


def beat(agent: str) -> None:
    _beats[agent] = time.time()


def beats() -> dict[str, float]:
    return dict(_beats)


def is_paused(agent: str) -> bool:
    return agent in paused


def pause(agent: str, open_positions: int = 0) -> None:
    if agent == "risk" and open_positions > 0:
        raise ValueError("Risk can't be paused while a position is open")
    paused.add(agent)


def resume(agent: str) -> None:
    paused.discard(agent)
