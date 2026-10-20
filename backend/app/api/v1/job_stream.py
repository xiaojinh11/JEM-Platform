"""
WebSocket endpoint for live job progress events.

Wire shape (matches ``services/job_stream.py``):

    client →  ws://host/api/v1/ws/jobs/{job_id}
              (Sec-WebSocket-Protocol: naw-token,<jwt>)
    server →  { "type": "step_update", "step_id": ..., ... }
              { "type": "log",          "device_id": ..., "log": "..." }
              { "type": "status_update","status": "succeeded", ... }
              { "type": "backlog_marker","last_seen_step_id": ... }

Authentication: JWT via ``Sec-WebSocket-Protocol`` header (same scheme
as the WebSSH endpoint).  All checks happen BEFORE ``accept()`` so a
bad token produces a clean close rather than a half-open socket.

Authorization rules (mirrors REST endpoints in ``jobs.py``):
  * admin  → may subscribe to any job
  * operator → may only subscribe to jobs they initiated
  * viewer → rejected with WS_1008_POLICY_VIOLATION
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from sqlalchemy.orm import Session

from app.core.logging import logger
from app.core.security import decode_token, is_revoked
from app.db.session import SessionLocal
from app.models.job import Job
from app.models.user import User
from app.services.job_stream import get_hub

router = APIRouter(tags=["job-stream"])


def _resolve_user(subprotocols: Optional[str]) -> Optional[tuple[int, str, str]]:
    """Return (user_id, username, raw_token) if JWT is valid, else None.

    Mirrors ``webssh._resolve_user`` exactly.
    """
    candidate: Optional[str] = None
    if subprotocols:
        s = subprotocols.strip()
        if s.startswith("naw-token,"):
            candidate = s[len("naw-token,"):].strip()
        elif "," in s:
            for raw in s.split(","):
                p = raw.strip()
                if p.startswith("eyJ") and "." in p:
                    candidate = p
                    break
        else:
            candidate = s
    if not candidate:
        return None
    try:
        payload = decode_token(candidate)
    except Exception as exc:  # noqa: BLE001
        logger.debug("job-stream token decode failed: %s", exc)
        return None
    if payload.get("typ") and payload.get("typ") != "access":
        return None
    if is_revoked(candidate):
        return None
    sub = payload.get("sub")
    if not sub:
        return None
    try:
        user_id = int(sub)
    except (TypeError, ValueError):
        return None
    username = str(payload.get("username") or "")
    return user_id, username, candidate


@router.websocket("/ws/jobs/{job_id}")
async def jobs_ws(
    websocket: WebSocket,
    job_id: int,
) -> None:
    # ── 1. Authenticate ─────────────────────────────────────────────
    subprotocols = websocket.headers.get("sec-websocket-protocol")
    resolved = _resolve_user(subprotocols)
    if not resolved:
        # Tell caller cleanly — no socket state leaked.
        try:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        except Exception:  # noqa: BLE001
            pass
        return
    user_id, username, _raw_token = resolved

    # ── 2. Authorize ────────────────────────────────────────────────
    db: Session = SessionLocal()
    try:
        user = db.get(User, user_id)
        if not user or not user.is_active:
            try:
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            except Exception:  # noqa: BLE001
                pass
            return

        if user.role == "viewer":
            logger.warning("job-stream rejected viewer user=%s job_id=%s", username, job_id)
            try:
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            except Exception:  # noqa: BLE001
                pass
            return

        # Job-level ownership check (admin bypasses it)
        job = db.get(Job, job_id)
        if not job:
            try:
                await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
            except Exception:  # noqa: BLE001
                pass
            return

        if user.role != "admin" and job.initiated_by != username:
            logger.warning("job-stream: user=%s tried to subscribe job=%s owned by %s",
                          username, job_id, job.initiated_by)
            try:
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            except Exception:  # noqa: BLE001
                pass
            return
    finally:
        db.close()

    # ── 3. Accept + stream ──────────────────────────────────────────
    await websocket.accept()
    logger.info("job-stream: user=%s subscribed job_id=%s", username, job_id)

    hub = get_hub()
    try:
        queue, backlog = await hub.subscribe(job_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("job stream subscribe failed: %s", exc)
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        return

    # Replay backlog so the operator's first frame shows everything
    # that happened before they connected.
    try:
        marker = {"type": "backlog_marker", "count": len(backlog)}
        await websocket.send_text(json.dumps(marker))
        for entry in backlog:
            await websocket.send_text(entry)
    except Exception:  # noqa: BLE001
        await hub.unsubscribe(job_id, queue)
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass
        return

    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                # Heartbeat — keeps the connection warm through proxies
                # that idle out unused WS sessions.
                await websocket.send_text(json.dumps({"type": "heartbeat"}))
                continue
            await websocket.send_text(event.to_json())
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("job stream ws loop error: %s", exc)
    finally:
        await hub.unsubscribe(job_id, queue)
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass
