"""Browser graphical console - WebSocket ↔ TCP bridge to QEMU's VNC.

noVNC (vendored under /static/novnc) speaks RFB over WebSocket; QEMU exposes
plain TCP VNC on the host. This endpoint bridges the two - the same role
websockify plays in Proxmox/OpenStack, in ~40 lines of asyncio.

Two deployment shapes share the same session core:
- control plane with HOST_MODE=local bridges to QEMU on this host;
- control plane with HOST_MODE=remote proxies the WebSocket to an agent, which
  bridges to QEMU next to libvirt.

WebSocket close codes must be 1000-1015 / 3000-4999 - HTTP-style codes are
invalid and crash the close frame, so we use the 44xx/45xx range.
"""
import asyncio
import ipaddress

from fastapi import APIRouter, WebSocket
from starlette.concurrency import run_in_threadpool

from app.ws_utils import serve_websocket

router = APIRouter(tags=["console"])


async def _tcp_to_ws(reader: asyncio.StreamReader, websocket: WebSocket):
    while True:
        data = await reader.read(65536)
        if not data:
            break  # TCP EOF - VNC server closed
        await websocket.send_bytes(data)


async def _ws_to_tcp(writer: asyncio.StreamWriter, websocket: WebSocket):
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            break
        payload = message.get("bytes")
        if payload:
            writer.write(payload)
            await writer.drain()


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in ("localhost",)


async def stream_vnc(websocket: WebSocket, vm_name: str, client) -> None:
    """Bridge an accepted WebSocket to QEMU's VNC socket."""
    vm = await run_in_threadpool(client.get_vm, vm_name)
    if vm is None:
        await websocket.close(code=4404, reason="VM not found")
        return
    if vm.state.lower() != "running":
        await websocket.close(code=4409, reason="VM is not running")
        return

    display = await run_in_threadpool(client.get_display, vm_name)
    if not display:
        await websocket.close(
            code=4501,
            reason="No VNC graphics on this VM (SPICE or none) - provision "
                   "via LabForge with GRAPHICS_TYPE=vnc",
        )
        return

    host, port = display
    if not _is_loopback(host):
        # Never proxy to arbitrary addresses from domain XML (SSRF guard).
        await websocket.close(code=4501, reason=f"Refusing non-loopback VNC address {host}")
        return

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=5
        )
    except (OSError, asyncio.TimeoutError):
        await websocket.close(code=4502, reason=f"Cannot reach VNC at {host}:{port}")
        return

    to_ws = asyncio.create_task(_tcp_to_ws(reader, websocket))
    to_tcp = asyncio.create_task(_ws_to_tcp(writer, websocket))
    try:
        await asyncio.wait({to_ws, to_tcp}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (to_ws, to_tcp):
            task.cancel()
        for task in (to_ws, to_tcp):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        writer.close()
        try:
            await writer.wait_closed()
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await websocket.close()
        except Exception:
            pass


@router.websocket("/ws/vnc/{vm_name}")
async def vnc_ws(websocket: WebSocket, vm_name: str):
    await serve_websocket(websocket, vm_name, f"/ws/vnc/{vm_name}", stream_vnc)
