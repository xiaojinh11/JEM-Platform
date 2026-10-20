"""FastAPI application entrypoint."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.adapters.registry import autodiscover
from app.core.config import settings
from app.core.logging import logger
from app.core.security import hash_password
from app.core.logging import configure_logging  # noqa: F401 - runs at import
from app.db.session import SessionLocal
from app.models.user import User
from app.api.v1 import api_router
from app.workers.drift_scheduler import start as start_drift_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("NAW starting up (env={}, debug={})", settings.env, settings.debug)
    autodiscover()

    # Initialise the embedded graph database (Kuzu) so the visualisation
    # panel has somewhere to read from. Schema creation is idempotent
    # and fast; if the file is missing we lazily create the directory.
    try:
        from app.graph import get_graph
        get_graph()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Graph init failed (visualisation will be empty): {}", exc)

    # Bootstrap admin if no users exist
    db = SessionLocal()
    try:
        if db.query(User).count() == 0:
            admin = User(
                username=settings.bootstrap_admin_username,
                password_hash=hash_password(settings.bootstrap_admin_password),
                role="admin",
                full_name="Administrator",
            )
            db.add(admin)
            db.commit()
            logger.info(
                "Bootstrap admin created: {} (password in .env)",
                settings.bootstrap_admin_username,
            )
    finally:
        db.close()

    # Start drift-detection background scheduler (no Redis required)
    start_drift_scheduler()

    yield

    logger.info("NAW shutting down")


app = FastAPI(
    title=settings.app_name,
    version=__version__,
    lifespan=lifespan,
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
    # ``redirect_slashes`` left at the FastAPI default (True). The frontend
    # intercepts collection URLs (``/devices`` -> ``/devices/``) before
    # they leave the browser, so we never hit a 307 in practice. Keeping
    # the default on means that even if a request slips through (e.g.
    # an external script, a curl from a developer), the API still works.
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.debug else [o.strip() for o in settings.allowed_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=settings.api_v1_prefix)


@app.get("/health")
def health():
    return {"version": __version__, "env": settings.env}


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("unhandled exception: {} {}: {}", request.method, request.url, exc)
    return JSONResponse(status_code=500, content={"detail": "internal server error"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=settings.debug)
