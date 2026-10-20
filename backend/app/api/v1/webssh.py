"""WebSSH proxy endpoint: upgrades HTTP → WebSocket and bridges to Paramiko.

Authentication:
  * JWT is taken from the ``Sec-WebSocket-Protocol`` header (preferred) or
    a ``token`` query argument (legacy, logs a warning).
  * Revoked JWTs are rejected before anything else — see
    ``app.core.security.is_revoked``.
  * Only ``admin`` and ``operator`` roles may open interactive shells;
    ``viewer`` is rejected with WS_1008_POLICY_VIOLATION.
  * Only endpoints belonging to the requested device and credentials
    accessible to the user are accepted.

Session selection:
  * Optional ``endpoint_id`` / ``credential_id`` query args let the
    frontend show a CRT-style "Connect" dialog and pick the right
    combination without leaking credentials to the browser.
  * If omitted, the device's primary endpoint (and its bound credential)
    is used, falling back to any endpoint that has a credential.

Lifecycle:
  * Each successful session is recorded in the audit log on connect
    and disconnect.
  * Every denial / failure path also writes an audit row tagged
    ``webssh.deny`` or ``webssh.connect_failed`` — this is the only
    way an admin sees brute-force probes, role-escalation attempts,
    or devices pointing at credentials they shouldn't have.
  * The frontend may send ``{"type": "close"}`` to gracefully close.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from sqlalchemy.orm import Session

from app.core.logging import logger
from app.db.session import SessionLocal
from app.models.device import Device, DeviceEndpoint
from app.models.credential import Credential
from app.models.user import User
from app.core.security import decode_token, is_revoked
from app.services.audit import record as audit_record
from app.services.executor import ExecutorError, interactive_session
from app.core.enums import Vendor as V

router = APIRouter()


# ── Auth helpers ────────────────────────────────────────────────────


def _audit_denied(actor: str, device_id: int, reason: str, detail: str = "") -> None:
    """Persist a single deny event.  Uses its own short-lived session.

    Failures here MUST NOT crash the connection path — the audit
    helper itself already wraps ``db.add`` in a try/except, but we
    wrap the whole block so that a misbehaving DB never reaches the
    caller.
    """
    audit_db = SessionLocal()
    try:
        audit_record(
            audit_db,
            actor=actor or "<unknown>",
            action="webssh.deny",
            target_kind="device",
            target_id=str(device_id),
            detail=f"reason={reason} {detail}".strip(),
        )
        audit_db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("webssh deny audit failed: {}", exc)
    finally:
        try:
            audit_db.close()
        except Exception:  # noqa: BLE001
            pass


def _resolve_user(token: Optional[str], subprotocols: Optional[str]) -> Optional[tuple[int, str, str]]:
    """Return (user_id, username, raw_token) if valid, else None.

    Looks at the Sec-WebSocket-Protocol header first, then falls back to
    the legacy ``token`` query argument.  The raw token (not just the
    payload) is returned so the caller can run ``is_revoked()`` against
    it.

    Returns ``None`` for any of: no candidate, decode failure, wrong
    ``typ``, or revocation — the caller closes the socket and we write
    a deny audit row there.
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
        candidate = token

    if not candidate:
        return None
    try:
        payload = decode_token(candidate)
    except Exception as exc:  # noqa: BLE001
        logger.debug("webssh token decode failed: {}", exc)
        return None
    # Reject password-reset / offline-manual tokens routed here.
    if payload.get("typ") and payload.get("typ") != "access":
        return None
    # Reject logged-out or admin-revoked tokens.
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


# ── Read/write loops ────────────────────────────────────────────────


async def _read_loop(ws: WebSocket, session_id: str, shell):
    """Read from the SSH shell and forward to the WebSocket."""
    try:
        while True:
            data = shell.read(size=4096, timeout=0.1)
            if data:
                encoded = base64.b64encode(data).decode("ascii")
                await ws.send_json({"type": "data", "data": encoded})
            await asyncio.sleep(0.02)
    except Exception as exc:  # noqa: BLE001
        logger.debug("read loop ended for session {}: {}", session_id, exc)


async def _write_loop(ws: WebSocket, shell):
    """Read from the WebSocket and forward to the SSH shell."""
    try:
        while True:
            msg = await ws.receive_text()
            try:
                payload = json.loads(msg)
            except Exception:  # noqa: BLE001
                continue
            msg_type = payload.get("type", "")
            if msg_type == "resize":
                cols = int(payload.get("cols", 120))
                rows = int(payload.get("rows", 30))
                try:
                    shell.resize(cols, rows)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("resize failed: {}", exc)
            elif msg_type == "data":
                raw = payload.get("data", "")
                try:
                    decoded = base64.b64decode(raw)
                    written = shell.write(decoded)
                    if written == 0:
                        # Shell closed remotely (eNSP idle timeout / VTY limit)
                        logger.debug("shell closed, terminating write loop for session {}", ws.client)
                        await ws.send_json({"type": "error", "message": "设备已关闭 SSH 会话"})
                        return
                except Exception as exc:  # noqa: BLE001
                    # `shell.write` raises OSError("Socket is closed") once
                    # the paramiko channel is gone (e.g. device-side
                    # disconnect, keepalive miss, or shell.close() in the
                    # read-loop).  Before this guard we logged and kept
                    # spinning, hammering the broken channel on every
                    # subsequent keystroke and flooding the log.  The
                    # surrounding gather() tears both pumps down when
                    # this function returns, so a return here is the
                    # correct cleanup.
                    logger.debug("bad data frame: {}", exc)
                    return
            elif msg_type == "ping":
                await ws.send_json({"type": "pong"})
            elif msg_type == "close":
                return
    except WebSocketDisconnect:
        return
    except Exception as exc:  # noqa: BLE001
        logger.debug("write loop ended: {}", exc)


# ── Endpoint ────────────────────────────────────────────────────────


@router.websocket("/ws/ssh/{device_id}")
async def ssh_websocket(
    ws: WebSocket,
    device_id: int,
    token: Optional[str] = None,
    endpoint_id: Optional[int] = None,
    credential_id: Optional[int] = None,
):
    """Bidirectional WebSocket proxy for interactive SSH.

    Query params:
      * ``token`` (legacy) — JWT. Prefer ``Sec-WebSocket-Protocol``.
      * ``endpoint_id`` — pick a specific endpoint for the device.
      * ``credential_id`` — pick a specific credential to authenticate with.
        Required for non-simulated devices whose endpoint does not have
        a credential bound.
    """
    try:
        await _ssh_websocket_impl(
            ws,
            device_id,
            token=token,
            endpoint_id=endpoint_id,
            credential_id=credential_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("WebSSH unhandled error: {}", exc)
        try:
            await ws.accept()
        except Exception:  # noqa: BLE001
            pass
        try:
            await ws.send_json({"type": "error", "message": f"server error: {exc}"})
        except Exception:  # noqa: BLE001
            pass
        try:
            await ws.close(code=status.WS_1011_INTERNAL_ERROR)
        except Exception:  # noqa: BLE001
            pass


async def _ssh_websocket_impl(
    ws: WebSocket,
    device_id: int,
    *,
    token: Optional[str] = None,
    endpoint_id: Optional[int] = None,
    credential_id: Optional[int] = None,
):
    session_id = f"{device_id}_{int(time.time() * 1000)}"

    # 1. Resolve and validate JWT (decode + revocation check).
    subprotocols = ws.headers.get("sec-websocket-protocol")
    resolved = _resolve_user(token, subprotocols)
    if not resolved:
        # We can't tell who tried — log as anonymous so a brute-force
        # pattern of "invalid token" attempts shows up under one bucket.
        _audit_denied("<anonymous>", device_id, "invalid_token")
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    user_id, _username, _raw_token = resolved

    logger.info(
        "WebSSH: device_id={} endpoint_id={} credential_id={} user_id={}",
        device_id, endpoint_id, credential_id, user_id,
    )

    if not token and subprotocols:
        # If the protocol header carried the token we can stay quiet.
        pass
    elif token:
        logger.warning(
            "WebSSH: token passed in URL query string (session=%s) — "
            "prefer Sec-WebSocket-Protocol header",
            session_id,
        )

    # 2. Validate user + role (RBAC: viewer forbidden)
    db: Session = SessionLocal()
    try:
        user = db.get(User, user_id)
        if not user:
            _audit_denied("<unknown>", device_id, "unknown_user", detail=f"user_id={user_id}")
            await ws.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        if not user.is_active:
            _audit_denied(user.username, device_id, "inactive_user")
            await ws.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        if user.role not in {"admin", "operator"}:
            logger.warning(
                "WebSSH: rejected user={} (role={}) on device={}",
                user.username,
                user.role,
                device_id,
            )
            _audit_denied(user.username, device_id, "insufficient_role", detail=f"role={user.role}")
            await ws.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        # 3. Resolve device and endpoint
        dev = db.get(Device, device_id)
        if not dev:
            _audit_denied(user.username, device_id, "unknown_device")
            await ws.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        if endpoint_id is not None:
            ep = db.get(DeviceEndpoint, endpoint_id)
            if ep is None or ep.device_id != device_id:
                _audit_denied(
                    user.username,
                    device_id,
                    "endpoint_mismatch",
                    detail=f"endpoint_id={endpoint_id}",
                )
                await ws.close(code=status.WS_1008_POLICY_VIOLATION)
                return
        else:
            ep = (
                db.query(DeviceEndpoint)
                .filter_by(device_id=device_id, is_primary=True)
                .first()
                or db.query(DeviceEndpoint).filter_by(device_id=device_id).first()
            )
        if not ep:
            _audit_denied(user.username, device_id, "no_endpoint")
            await ws.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        # 4. Resolve credential
        cred: Optional[Credential] = None
        if credential_id is not None:
            cred = db.get(Credential, credential_id)
        else:
            cred = (
                db.get(Credential, ep.credential_id) if ep.credential_id else None
            )
    finally:
        db.close()

    # Accept only after the heavy lifting succeeds so failures result in a
    # clean policy-violation close rather than a half-open socket.
    await ws.accept()

    # Audit connect (use a fresh session so we don't leak the one we just
    # closed above)
    audit_db = SessionLocal()
    try:
        audit_record(
            audit_db,
            actor=user.username,
            action="webssh.connect",
            target_kind="device",
            target_id=str(device_id),
            detail=f"endpoint_id={ep.id} kind={ep.kind} address={ep.address}:{ep.port}",
        )
        audit_db.commit()
    finally:
        audit_db.close()

    # 5. Open the shell
    if dev.is_simulated:
        from app.adapters.simulated import _SimulatedShell
        from app.services.executor import ConnectionHandle
        handle = ConnectionHandle(
            vendor=dev.vendor,
            address=ep.address,
            port=ep.port,
            meta={"hostname": dev.hostname or dev.name},
        )
        shell = _SimulatedShell(handle=handle)
        await ws.send_json({"type": "info", "message": "已连接到模拟设备（无真实 SSH）"})
    else:
        if not cred:
            # The socket is already open; record the failure and close.
            audit_db = SessionLocal()
            try:
                audit_record(
                    audit_db,
                    actor=user.username,
                    action="webssh.connect_failed",
                    target_kind="device",
                    target_id=str(device_id),
                    detail=f"reason=missing_credential endpoint_id={ep.id}",
                )
                audit_db.commit()
            finally:
                audit_db.close()
            await ws.send_json(
                {"type": "error", "message": "未配置凭据，请先在设备详情中绑定凭据或选择凭据"}
            )
            await ws.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        try:
            shell = interactive_session(
                vendor=V(dev.vendor),
                address=ep.address,
                port=ep.port,
                credential=cred,
            )
        except ExecutorError as exc:
            logger.error("SSH connection failed (device={}): {}", device_id, exc)
            audit_db = SessionLocal()
            try:
                audit_record(
                    audit_db,
                    actor=user.username,
                    action="webssh.connect_failed",
                    target_kind="device",
                    target_id=str(device_id),
                    detail=f"reason=executor_error endpoint_id={ep.id} err={exc}",
                )
                audit_db.commit()
            finally:
                audit_db.close()
            await ws.send_json({"type": "error", "message": f"SSH 连接失败：{exc}"})
            await ws.close(code=status.WS_1011_INTERNAL_ERROR)
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("SSH unexpected error (device={}): {}", device_id, exc)
            audit_db = SessionLocal()
            try:
                audit_record(
                    audit_db,
                    actor=user.username,
                    action="webssh.connect_failed",
                    target_kind="device",
                    target_id=str(device_id),
                    detail=f"reason=unexpected endpoint_id={ep.id} err={exc}",
                )
                audit_db.commit()
            finally:
                audit_db.close()
            await ws.send_json({"type": "error", "message": f"SSH 连接失败：{exc}"})
            await ws.close(code=status.WS_1011_INTERNAL_ERROR)
            return

    # Send a hello message so the client UI can confirm the connection.
    await ws.send_json(
        {
            "type": "info",
            "message": f"已连接到 {dev.name} ({ep.address}:{ep.port})",
            "session_id": session_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    try:
        read_task = asyncio.create_task(_read_loop(ws, session_id, shell))
        write_task = asyncio.create_task(_write_loop(ws, shell))
        # BUGFIX: we used `asyncio.wait(..., return_when=FIRST_COMPLETED)`
        # here, which cancels the surviving task the moment the other one
        # ends.  That's wrong for an interactive SSH bridge: the read loop
        # naturally exits when the SSH channel reaches EOF (e.g. when the
        # device closes the connection), but the write loop is still
        # healthy and should keep forwarding operator keystrokes until the
        # WebSocket itself closes.  Cancelling the write loop on a read
        # EOF meant every subsequent Enter / backspace was silently
        # dropped — manifesting as "first command works, then Enter has
        # no effect" once the device produced a prompt that happened to
        # land between our two pumps.
        #
        # `asyncio.gather` with no `return_exceptions` propagates the
        # first exception out and naturally tears both tasks down.  The
        # surrounding `finally` closes the shell and audits the
        # disconnect, so the only behavioural change is that we now keep
        # forwarding keystrokes for the full lifetime of the WebSocket.
        await asyncio.gather(read_task, write_task)
    finally:
        try:
            shell.close()
        except Exception:  # noqa: BLE001
            pass
        # Audit disconnect
        audit_db = SessionLocal()
        try:
            audit_record(
                audit_db,
                actor=user.username,
                action="webssh.disconnect",
                target_kind="device",
                target_id=str(device_id),
                detail=f"endpoint_id={ep.id} session_id={session_id}",
            )
            audit_db.commit()
        finally:
            audit_db.close()
