"""API v1 router registry.

We flatten every sub-router's routes directly into the top-level
``api_router``.  Because ``router.routes[i].path`` semantics depend on
whether the inner router has a prefix, we compute the absolute path as:

    inner_path = router.prefix + r.path   (works for both empty and non-empty)

The top-level mount in ``app/main.py`` then applies ``/api/v1`` once.

NB: this registry handles BOTH ``APIRoute`` (REST) and ``APIWebSocketRoute``
(WebSocket).  The previous version only matched ``APIRoute``, which silently
dropped every ``@router.websocket(...)`` endpoint — that was the reason
WebSSH never worked even though the inner router existed.
"""
from fastapi import APIRouter
from fastapi.routing import APIRoute, APIWebSocketRoute

from app.api.v1 import (
    auth, account, dashboard, devices, credentials, configs, documents,
    offline_manual, jobs, job_approval, job_export, job_stream, audit,
    templates, system, webssh, drift, ai, graph, export, health,
    health_schedules, commands,
)


def _build_router(router, *, prefix: str = "", tags: list[str]):
    """Flatten every leaf route into the top-level router, prepending
    ``prefix`` to each path so the frontend can hit
    ``/api/v1/<prefix>/<endpoint>``.

    Each sub-module's router is created as a bare ``APIRouter()`` (no prefix)
    so that authors only need to think about the relative path on each
    @router.*  decorator — the module name drives the absolute path here.

    Handles both REST (``APIRoute``) and WebSocket (``APIWebSocketRoute``)
    endpoints.
    """
    norm = prefix.strip("/")
    for r in router.routes:
        if isinstance(r, APIRoute):
            inner = r.path if r.path.startswith("/") else "/" + r.path
            full = ("/" + norm + inner) if norm else inner
            api_router.add_api_route(
                full,
                r.endpoint,
                methods=list(r.methods or []),
                tags=tags + (list(r.tags or []) if r.tags else []),
                response_model=r.response_model,
                status_code=r.status_code,
                dependencies=list(r.dependencies) if r.dependencies else None,
                summary=r.summary,
                description=r.description,
            )
        elif isinstance(r, APIWebSocketRoute):
            inner = r.path if r.path.startswith("/") else "/" + r.path
            full = ("/" + norm + inner) if norm else inner
            # FastAPI's ``add_api_websocket_route`` is the FastAPI-aware
            # version that knows how to inject path params (e.g.
            # ``device_id``) into the handler — the plain
            # ``add_websocket_route`` is a Starlette shim that does NOT
            # do that, which is why the original code got
            # ``missing argument 'device_id'`` errors.
            api_router.add_api_websocket_route(
                full,
                r.endpoint,
                dependencies=list(r.dependencies) if r.dependencies else None,
                name=r.name,
            )


api_router = APIRouter()

# Each sub-router is a bare APIRouter(); the second positional argument
# is the path prefix used in the absolute URL (e.g. ``/api/v1/auth/...``).
# Centralising the prefix here prevents the historical "forgot to add a
# prefix" bug where the frontend's `/api/v1/auth/...` requests fell through
# to FastAPI's root and 404'd.
_build_router(auth.router,           prefix="auth",           tags=["auth"])
_build_router(account.router,        prefix="auth",           tags=["auth"])
_build_router(dashboard.router,      prefix="dashboard",      tags=["dashboard"])
_build_router(devices.router,        prefix="devices",        tags=["devices"])
_build_router(credentials.router,    prefix="credentials",    tags=["credentials"])
_build_router(configs.router,        prefix="configs",        tags=["configs"])
_build_router(offline_manual.router, prefix="offline-manual", tags=["offline-manual"])
_build_router(documents.router,      prefix="documents",      tags=["documents"])
_build_router(templates.router,      prefix="templates",      tags=["templates"])
_build_router(jobs.router,           prefix="jobs",           tags=["jobs"])
_build_router(job_export.router,     prefix="jobs",           tags=["jobs"])
_build_router(job_approval.router,   prefix="job-approval",   tags=["job-approval"])
# job_stream has no REST routes — only WebSocket — so its prefix is
# the URL fragment ``/api/v1/ws/jobs/...`` directly.
_build_router(job_stream.router,     prefix="",               tags=["job-stream"])
_build_router(audit.router,          prefix="audit",          tags=["audit"])
_build_router(system.router,         prefix="system",         tags=["system"])
_build_router(webssh.router,         prefix="",               tags=["webssh"])
_build_router(drift.router,          prefix="",               tags=["drift"])
_build_router(ai.router,             prefix="",               tags=["ai"])
_build_router(graph.router,          prefix="graph",          tags=["graph"])
_build_router(export.router,         prefix="",               tags=["export"])
_build_router(health.router,         prefix="health",         tags=["health"])
# health_schedules is mounted under ``/health/schedules`` so the
# collection endpoints (``POST /api/v1/health/schedules``) don't
# collide with the bare ``/health`` prefix used by ``health.py``'s
# ``/health/collect`` / ``/health/snapshots`` family.
_build_router(health_schedules.router, prefix="health/schedules", tags=["health-schedules"])
# Custom command collection (migrated from PythonProject5). Lives
# under ``/commands/...`` so the URL space stays clear of the
# existing ``/configs`` and ``/jobs`` families.
_build_router(commands.router,         prefix="commands",         tags=["commands"])
