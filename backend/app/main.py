"""LabForge - lightweight KVM lab provisioner.

One codebase, two entrypoints:
  MODE=control  serves the UI/API (this module's ``create_app``);
  MODE=agent    runs next to libvirt and owns host operations.

With ``LOCAL_AGENT=true`` the control plane starts the agent in a background
thread on loopback and points the host client at it, so a single systemd unit
runs both roles.
"""
import logging
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import vm_metrics
from app.agent import create_agent_app
from app.api import v1 as api_v1
from app.config import APP_VERSION, settings
from app.console import router as console_router
from app.host_client import get_host_client, host_mode
from app.ui import router as ui_router, templates
from app.virsh_client import OutsideLabError
from app.vnc import router as vnc_router

logger = logging.getLogger("labforge")


_agent_server: uvicorn.Server | None = None  # Set during startup if LOCAL_AGENT


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _agent_server
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info("%s starting (mode=%s, host_mode=%s, templates=%s, storage=%s)",
                settings.app_name, settings.mode, host_mode(),
                settings.templates_dir, settings.vm_storage_path)
    # The sampler runs where libvirt is local: control+local, or the agent.
    # With LOCAL_AGENT the in-process agent owns the sampler.
    if not settings.local_agent and host_mode() == "local":
        vm_metrics.start_sampler()
    yield
    vm_metrics.stop_sampler()
    if _agent_server is not None:
        logger.info("Shutting down in-process agent server...")
        _agent_server.shutdown()
    logger.info("%s shutting down", settings.app_name)


def _start_local_agent() -> tuple[threading.Thread, uvicorn.Server]:
    """Run the agent in a daemon thread and point the control plane at it.

    A random token is generated when none is configured so the in-process split
    is authenticated like a real remote agent would be.
    Returns (thread, server) so the lifespan can shut the server down cleanly.
    """
    if not settings.agent_bind_is_loopback:
        logger.warning("LOCAL_AGENT forces the in-process agent onto 127.0.0.1")
        settings.agent_bind = "127.0.0.1"
    if not settings.agent_token:
        settings.agent_token = secrets.token_urlsafe(32)
    settings.host_mode = "remote"
    settings.agent_url = settings.api_base()

    agent_app = create_agent_app()
    config = uvicorn.Config(agent_app, host=settings.agent_bind,
                            port=settings.agent_port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="labforge-agent", daemon=True)
    thread.start()

    # Wait until the in-process agent answers before serving traffic.
    import httpx

    for _ in range(50):
        try:
            if httpx.get(f"{settings.agent_url}/agent/v1/health",
                         timeout=0.5).status_code == 200:
                logger.info("In-process agent ready at %s", settings.agent_url)
                return thread, server
        except Exception:  # noqa: BLE001 - startup poll, keep trying
            pass
        time.sleep(0.2)
    logger.error("In-process agent did not become ready at %s", settings.agent_url)
    return thread, server


def create_app() -> FastAPI:
    # MODE=agent runs the host-side service; MODE=control serves the UI/API.
    if settings.mode == "agent":
        return create_agent_app()

    global _agent_server
    if settings.local_agent:
        thread, server = _start_local_agent()
        _agent_server = server

    app = FastAPI(
        title=settings.app_name,
        version=APP_VERSION,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
    )

    @app.exception_handler(OutsideLabError)
    async def outside_lab(request: Request, exc: OutsideLabError):
        return JSONResponse(status_code=404, content={"detail": "Lab VM not found"})

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        """Render an HTML error page for browser requests, JSON for APIs."""
        path = request.url.path
        wants_json = path.startswith(("/api", "/agent", "/ws"))
        accept = request.headers.get("accept", "")
        if not wants_json and "text/html" in accept:
            return templates.TemplateResponse(
                request,
                "error.html",
                {"status": exc.status_code, "detail": exc.detail, "active_page": ""},
                status_code=exc.status_code,
            )
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception):
        # Log the detail server-side; never leak internals to the client.
        logger.exception("Unhandled error handling %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    app.include_router(api_v1.router, prefix="/api/v1")
    app.include_router(ui_router)
    app.include_router(console_router)
    app.include_router(vnc_router)

    @app.get("/api/health")
    async def health():
        """Liveness - the process is up."""
        return {"status": "healthy", "app": settings.app_name}

    @app.get("/api/ready")
    def readiness():
        """Readiness - the host is reachable and templates are present."""
        checks = {"host": False, "templates": False}
        try:
            checks["host"] = get_host_client().ping()
        except Exception as exc:  # noqa: BLE001 - readiness must never raise
            logger.warning("Readiness: host check failed: %s", exc)
        checks["templates"] = Path(settings.templates_dir).is_dir()
        ready = all(checks.values())
        return JSONResponse(status_code=200 if ready else 503,
                            content={"ready": ready, "host_mode": host_mode(),
                                     "checks": checks})

    return app


app = create_app()
