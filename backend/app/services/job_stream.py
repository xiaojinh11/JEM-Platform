"""
Job progress streaming hub — bridges Celery worker progress events to
WebSocket clients in the FastAPI process.

Why this exists
---------------

``batch_apply`` / ``batch_collect`` / ``template_apply`` are Celery
tasks; they run in a **different process** from the FastAPI app
serving WebSocket connections.  An in-memory registry (like the one
in ``services/notifier.py``) only works for *single-process* setups
and silently drops every job progress event in any deployment with
`celery worker` running separately.

The fix is to use Redis pub/sub as the inter-process bus:

    Celery worker process                       FastAPI process
    ─────────────────────                       ───────────────
    ``record_step(...)``                        ``JobStreamHub``
            │                                           ▲
            ▼                                           │
       redis.publish("naw:jobs:{id}:events", …)    pubsub.listen()
                                                       │
                                                       ▼
                                                 ws.send_text(...)

For an in-process fallback (Celery eager mode, single-worker
local dev), we still maintain an in-memory queue and replay missed
events to a freshly-connected client — see ``backlog``.

Event format
------------

    {
      "type": "step_update" | "status_update" | "log" | "backlog_marker",
      "job_id": 42,
      "device_id": 7,                # only for step_update / log
      "step_id": 123,
      "step_kind": "EXECUTE",        # PRECHECK / BACKUP_BEFORE / EXECUTE / BACKUP_AFTER
      "status": "running",           # pending / running / succeeded / failed / needs_verification
      "error": null | "...",
      "log": "interface Vlanif 99\\n...",
      "ts": "2026-09-25T21:30:11Z"
    }

The ``log`` field carries the actual command output of one EXECUTE
step (truncated to 2KB upstream).  ``status_update`` is reserved for
job-level transitions (running → succeeded, etc.) and is pushed from
the task's outermost try/finally.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

import redis.asyncio as redis_async
from redis.exceptions import RedisError

from app.core.config import settings
from app.core.logging import logger


_CHANNEL_PREFIX = "naw:jobs:"
"""Pub/sub channel name is ``naw:jobs:{job_id}:events``."""


@dataclass
class StreamEvent:
    """One progress event.  ``payload`` is the dict we JSON-encode and
    send to subscribers; the dataclass wrapper exists so the API
    endpoint can re-send backlog with the same code path as live events.
    """
    job_id: int
    payload: dict

    def to_json(self) -> str:
        return json.dumps(self.payload, ensure_ascii=False, default=str)


class JobStreamHub:
    """Process-wide singleton.  Holds:

    * an in-memory **backlog** per job (so a freshly-arriving WebSocket
      can replay what happened *before* it connected — without this the
      user opens the panel mid-run and sees an empty screen until the
      next step),
    * an asyncio ``pubsub`` subscription to the Redis bus (one per
      process; each subscriber is a coroutine that fan-outs to its
      attached WebSockets).

    Concurrency model: a single Redis connection runs the listen loop;
    each ``subscribe(job_id, ws)`` call starts a per-ws task that
    receives events from a queue fed by the listen loop.
    """

    # Per-job backlog cap.  At ~250 bytes per event, 500 = 125KB per
    # job in memory, which is fine even with hundreds of concurrent
    # jobs — and bounded by deque(maxlen=).
    BACKLOG_SIZE = 500

    def __init__(self) -> None:
        # job_id → deque[StreamEvent]
        self._backlog: dict[int, deque] = {}
        # job_id → set[asyncio.Queue[StreamEvent]]
        self._subscribers: dict[int, set] = {}
        # The Redis pubsub connection; lazily initialized on first use
        # because Redis may not be reachable at import time (e.g. unit
        # tests don't start it).
        self._redis: Optional[redis_async.Redis] = None
        self._pubsub = None
        self._listener_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    # ── public API ────────────────────────────────────────────────
    async def subscribe(self, job_id: int) -> tuple[asyncio.Queue, list[str]]:
        """Attach to a job's event stream.

        Returns ``(queue, backlog_json)``: the caller pops the queue
        in its read loop and also sends each entry of
        ``backlog_json`` down the WebSocket so the client catches up
        on anything that happened before it connected.
        """
        async with self._lock:
            if job_id not in self._backlog:
                self._backlog[job_id] = deque(maxlen=self.BACKLOG_SIZE)
                self._subscribers[job_id] = set()
                # First subscriber for this job — start listening on
                # the Redis channel.  The listener is a single
                # process-wide coroutine that loops over the
                # pubsub.listen() generator and fan-outs events.
                await self._ensure_listener()

        # Replay backlog *outside* the lock so a slow ws.send doesn't
        # hold up other subscribers joining.
        backlog = [e.to_json() for e in self._backlog[job_id]]

        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers[job_id].add(q)
        return q, backlog

    async def unsubscribe(self, job_id: int, q: asyncio.Queue) -> None:
        subs = self._subscribers.get(job_id)
        if subs:
            subs.discard(q)
            if not subs:
                # Last subscriber left — drop the per-job state.  The
                # listener stays alive (it's process-global).
                async with self._lock:
                    if job_id in self._subscribers and not self._subscribers[job_id]:
                        del self._subscribers[job_id]
                        self._backlog.pop(job_id, None)

    async def publish(self, job_id: int, payload: dict) -> None:
        """Push an event into the bus.  Called from inside the Celery
        task (sync) — see ``publish_sync`` for the synchronous shim.
        """
        event = StreamEvent(job_id=job_id, payload=payload)
        # Always keep the backlog so latecomers can replay.
        if job_id not in self._backlog:
            self._backlog[job_id] = deque(maxlen=self.BACKLOG_SIZE)
        self._backlog[job_id].append(event)

        # Fan-out to in-process subscribers (immediate).
        for q in list(self._subscribers.get(job_id, ())):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Slow consumer — drop the oldest in its queue to
                # make room.  This is acceptable because the client
                # will eventually catch up via the backlog; what we
                # cannot do is block the publisher.
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    pass

        # Broadcast to Redis so other processes can pick it up.
        if self._redis is None:
            await self._ensure_redis()
        if self._redis is not None:
            try:
                await self._redis.publish(
                    f"{_CHANNEL_PREFIX}{job_id}:events",
                    event.to_json(),
                )
            except RedisError as exc:
                logger.warning("job stream publish failed: %s", exc)

    # ── internal ──────────────────────────────────────────────────
    async def _ensure_redis(self) -> None:
        if self._redis is not None:
            return
        try:
            self._redis = redis_async.from_url(
                settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
            )
            await self._redis.ping()
        except Exception as exc:  # noqa: BLE001
            logger.warning("job stream: redis unavailable, falling back to in-process only: %s", exc)
            self._redis = None

    async def _ensure_listener(self) -> None:
        # Lazily start a single process-wide listener that subscribes
        # to *all* job channels via pattern subscription.
        if self._listener_task is not None and not self._listener_task.done():
            return
        if self._redis is None:
            await self._ensure_redis()
        if self._redis is None:
            return  # No Redis → no cross-process events.  In-process subscribers still work.
        try:
            self._pubsub = self._redis.pubsub()
            await self._pubsub.psubscribe(f"{_CHANNEL_PREFIX}*:events")
        except RedisError as exc:
            logger.warning("job stream psubscribe failed: %s", exc)
            return
        self._listener_task = asyncio.create_task(self._listen_loop())

    async def _listen_loop(self) -> None:
        assert self._pubsub is not None
        try:
            async for message in self._pubsub.listen():
                if message is None or message.get("type") != "pmessage":
                    continue
                channel = message.get("channel", "")
                data = message.get("data")
                if not isinstance(data, str):
                    continue
                # Channel format: naw:jobs:{id}:events
                parts = channel.split(":")
                if len(parts) < 4:
                    continue
                try:
                    job_id = int(parts[2])
                except (ValueError, IndexError):
                    continue
                # Re-parse to fan-out — we re-encode to keep
                # ``to_dict`` semantics consistent.
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    continue
                # Update backlog + push to local subscribers.
                event = StreamEvent(job_id=job_id, payload=payload)
                if job_id not in self._backlog:
                    self._backlog[job_id] = deque(maxlen=self.BACKLOG_SIZE)
                self._backlog[job_id].append(event)
                for q in list(self._subscribers.get(job_id, ())):
                    try:
                        q.put_nowait(event)
                    except asyncio.QueueFull:
                        try:
                            q.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                        try:
                            q.put_nowait(event)
                        except asyncio.QueueFull:
                            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("job stream listen loop exited: %s", exc)


# ── Synchronous shim for Celery workers ────────────────────────────
# Celery tasks run in a worker process that does not have an asyncio
# event loop attached by default.  ``publish_sync`` uses a fresh
# loop just to forward the event to Redis.  We *do not* try to
# maintain an in-memory hub in the worker — the worker has no
# WebSocket clients; the FastAPI processes do.

_sync_redis = None


def _get_sync_redis():
    """Return a process-local sync Redis client (lazy)."""
    global _sync_redis
    if _sync_redis is None:
        try:
            import redis  # type: ignore
        except ImportError:  # pragma: no cover
            return None
        try:
            _sync_redis = redis.from_url(settings.redis_url, decode_responses=True)
            _sync_redis.ping()
        except Exception as exc:  # noqa: BLE001
            logger.warning("job stream: sync redis unavailable: %s", exc)
            _sync_redis = None
    return _sync_redis


def publish_sync(job_id: int, payload: dict) -> None:
    """Celery-side entrypoint.  Idempotent + non-blocking.

    Falls back to ``logger.info`` if Redis is not available so the
    job still runs to completion even when the streaming pipeline is
    disabled (e.g. unit tests, ephemeral containers).
    """
    r = _get_sync_redis()
    if r is None:
        return
    payload_with_ts = {**payload, "ts": payload.get("ts") or int(time.time())}
    try:
        r.publish(f"{_CHANNEL_PREFIX}{job_id}:events", json.dumps(payload_with_ts, ensure_ascii=False, default=str))
    except Exception as exc:  # noqa: BLE001
        logger.warning("job stream publish_sync failed: %s", exc)


# ── singleton accessor ────────────────────────────────────────────
# Imported by both the WS endpoint (async) and the test fixtures
# (sync).  We expose ``hub`` directly rather than via a getter so
# mocking in tests is straightforward.
import asyncio as _asyncio

if "_hub_instance" not in globals():
    _hub_instance: Optional[JobStreamHub] = None


def get_hub() -> JobStreamHub:
    """Process-global ``JobStreamHub``.

    We do not expose the raw instance via ``from app.services.job_stream import hub``
    because that would be evaluated at import time before asyncio has
    a chance to set up a loop.  Going through this accessor gives a
    stable reference.
    """
    global _hub_instance
    if _hub_instance is None:
        _hub_instance = JobStreamHub()
    return _hub_instance
