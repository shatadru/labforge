"""LabForge agent - the host-side service.

Runs next to libvirt on a KVM host and exposes the host API the control plane
needs: a health and info probe, an allow-listed RPC endpoint over the existing
``VirshClient``, qcow2 image upload, and the serial and graphical console
WebSockets. Auth is a bearer token; TLS is expected in front of it (a private
CA or a pinned certificate). It holds no control-plane state.

The method allow-list here is the security boundary: the control plane can only
call the names in ``host_client.RPC_METHODS``.
"""
import dataclasses
import hmac
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Header, HTTPException, UploadFile, WebSocket

from app import vm_metrics
from app.config import APP_VERSION, settings
from app.console import stream_console
from app.host_client import RPC_METHODS
from app.image_store import store_upload
from app.virsh_client import get_virsh_client
from app.vnc import stream_vnc

logger = logging.getLogger("labforge.agent")


def _jsonable(value):
    """Convert dataclasses, Paths and containers into JSON-safe values."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: _jsonable(getattr(value, field.name))
                for field in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {key: _jsonable(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(val) for val in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _bearer_ok(authorization: str | None) -> bool:
    """Constant-time comparison of the Bearer token, if one is configured."""
    token = settings.agent_token
    if not token:
        return bool(settings.agent_allow_anonymous)
    expected = f"Bearer {token}"
    return hmac.compare_digest(authorization or "", expected)


def _check_token(authorization: str | None) -> None:
    """Fail-closed bearer auth. Anonymous access requires an explicit opt-in."""
    if not _bearer_ok(authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _check_ws_token(websocket: WebSocket) -> bool:
    """WebSocket auth. Header only - tokens in query strings leak via logs."""
    return _bearer_ok(websocket.headers.get("authorization"))


def _rpc_table(client) -> dict:
    return {
        "get_vm": client.get_vm,
        "list_vms": client.list_vms,
        "get_vm_user": client.get_vm_user,
        "vm_exists": client.vm_exists,
        "ping": client.ping,
        "list_templates": client.list_templates,
        "get_template": client.get_template,
        "tailscale_available": client.tailscale_available,
        "list_imported_images": client.list_imported_images,
        "list_snapshots": client.list_snapshots,
        "provision_name": client.provision_name,
        "create_vm": client.create_vm,
        "delete_vm": client.delete_vm,
        "start_vm": client.start_vm,
        "stop_vm": client.stop_vm,
        "reboot_vm": client.reboot_vm,
        "force_stop_vm": client.force_stop_vm,
        "create_snapshot": client.create_snapshot,
        "revert_snapshot": client.revert_snapshot,
        "delete_snapshot": client.delete_snapshot,
        "delete_imported_image": client.delete_imported_image,
        "agent_command": client.agent_command,
        "guest_exec_capture": client.guest_exec_capture,
        "get_vm_memory_stats": client.get_vm_memory_stats,
        "get_vm_usage": lambda name: vm_metrics.sampler.get(name),
        "all_vm_usage": lambda: vm_metrics.sampler.snapshot(),
    }


def _assert_rpc_table_matches_allowlist(client) -> dict:
    """Keep the dispatch table and the RPC allow-list in lockstep.

    Both are security boundaries; a mismatch means a method is either silently
    unreachable or silently refused, so fail loudly at startup instead.
    """
    table = _rpc_table(client)
    missing = set(RPC_METHODS) - set(table)
    extra = set(table) - set(RPC_METHODS)
    if missing or extra:
        raise RuntimeError(
            f"RPC registry drift: missing={sorted(missing)} extra={sorted(extra)}"
        )
    return table


def create_agent_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("LabForge agent starting (bind=%s:%s)",
                    settings.agent_bind, settings.agent_port)
        if not settings.agent_token and not settings.agent_allow_anonymous:
            logger.error(
                "AGENT_TOKEN is not set: all agent requests will be rejected. "
                "Set AGENT_TOKEN, or AGENT_ALLOW_ANONYMOUS=true for local dev."
            )
        vm_metrics.start_sampler()
        yield
        vm_metrics.stop_sampler()
        logger.info("LabForge agent shutting down")

    app = FastAPI(title="LabForge agent", lifespan=lifespan,
                  docs_url="/agent/docs", openapi_url="/agent/openapi.json")
    client = get_virsh_client()
    table = _assert_rpc_table_matches_allowlist(client)

    @app.get("/agent/v1/health")
    def health():
        return {"status": "healthy", "role": "agent",
                "name": settings.agent_name or os.uname().nodename}

    @app.get("/agent/v1/info")
    def info(authorization: str | None = Header(default=None)):
        _check_token(authorization)
        return {
            "name": settings.agent_name or os.uname().nodename,
            "advertise_url": settings.agent_advertise_url,
            "hostname": os.uname().nodename,
            "version": APP_VERSION,
            "libvirt": client.ping(),
            "templates": [t.name for t in client.list_templates()],
            "tailscale_available": client.tailscale_available(),
        }

    @app.post("/agent/v1/rpc/{method}")
    def rpc(method: str, body: dict, authorization: str | None = Header(default=None)):
        _check_token(authorization)
        if method not in RPC_METHODS:
            raise HTTPException(status_code=404, detail=f"Method '{method}' not allowed")
        fn = table[method]
        args = body.get("args", [])
        kwargs = body.get("kwargs", {})
        try:
            result = fn(*args, **kwargs)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - log detail, never leak a traceback
            logger.warning("RPC %s failed: %s", method, exc)
            raise HTTPException(status_code=500, detail="Host operation failed")
        return {"result": _jsonable(result)}

    @app.post("/agent/v1/images")
    async def upload_image(file: UploadFile = File(...),
                           authorization: str | None = Header(default=None)):
        _check_token(authorization)
        directory = settings.resolved_import_dir
        max_bytes = settings.max_upload_gb * (1024 ** 3)
        return await store_upload(file, directory, max_bytes, client.inspect_image)

    @app.websocket("/agent/v1/ws/console/{vm_name}")
    async def agent_console(websocket: WebSocket, vm_name: str):
        if not _check_ws_token(websocket):
            await websocket.close(code=4401, reason="Unauthorized")
            return
        await websocket.accept()
        await stream_console(websocket, vm_name, client)

    @app.websocket("/agent/v1/ws/vnc/{vm_name}")
    async def agent_vnc(websocket: WebSocket, vm_name: str):
        if not _check_ws_token(websocket):
            await websocket.close(code=4401, reason="Unauthorized")
            return
        await websocket.accept()
        await stream_vnc(websocket, vm_name, client)

    return app
