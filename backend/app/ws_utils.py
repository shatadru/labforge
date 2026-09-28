"""Shared WebSocket plumbing for the serial and graphical consoles.

Both consoles need the same gate: the feature must be enabled, the browser
Origin must match the Host (cross-site WebSocket hijacking guard), and then the
connection either runs locally or is proxied to an agent in remote mode.
"""
from urllib.parse import urlparse

from fastapi import WebSocket

from app.config import settings
from app.host_client import HostError, get_host_client, host_mode


def origin_allowed(websocket: WebSocket) -> bool:
    """Reject cross-site WebSocket hijacking (browsers send Origin on WS)."""
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host")
    if not origin:
        return True  # non-browser client (CLI/tests)
    return bool(host) and urlparse(origin).netloc == host


async def serve_websocket(websocket: WebSocket, vm_name: str, proxy_path: str,
                          local_stream) -> None:
    """Run or proxy a console WebSocket after the shared access checks."""
    if not settings.console_enabled:
        await websocket.close(code=4403, reason="Console disabled")
        return

    if not origin_allowed(websocket):
        await websocket.close(code=4403, reason="Cross-origin WebSocket rejected")
        return

    try:
        client = get_host_client()
    except HostError as exc:
        await websocket.close(code=4502, reason=f"Agent unreachable: {exc}")
        return

    await websocket.accept()

    if host_mode() == "remote":
        from app.ws_proxy import proxy_websocket
        await proxy_websocket(websocket, client, proxy_path)
        return

    await local_stream(websocket, vm_name, client)
