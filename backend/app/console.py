"""LabForge console - WebSocket PTY for virsh console.

Spawns `virsh console <vm>` in a PTY and streams I/O to xterm.js via WebSocket.

Two deployment shapes share the same session core:
- control plane with HOST_MODE=local runs the PTY here;
- control plane with HOST_MODE=remote proxies the WebSocket to an agent, which
  runs the PTY next to libvirt.
"""
import asyncio
import fcntl
import os
import pty
import signal
import struct
import termios

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.session_manager import session_manager
from app.ws_utils import serve_websocket

router = APIRouter(tags=["console"])


def _try_acquire_session(vm_name: str) -> bool:
    return session_manager.acquire(vm_name)


def _release_session(vm_name: str) -> None:
    session_manager.release(vm_name)


def _set_winsize(fd: int, rows: int, cols: int):
    """Set terminal window size."""
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


def _terminate_child(pid: int) -> None:
    """Stop the `virsh console` child, escalating to SIGKILL if it lingers."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except ChildProcessError:
        return
    for _ in range(10):
        try:
            done, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return
        if done:
            return
        try:
            os.kill(pid, signal.SIGKILL)
            break
        except ProcessLookupError:
            return
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


async def stream_console(websocket: WebSocket, vm_name: str, client) -> None:
    """Run a serial console session over an already accepted WebSocket."""
    vm = await run_in_threadpool(client.get_vm, vm_name)
    if vm is None:
        await websocket.send_json({"type": "error", "message": "VM not found"})
        await websocket.close(code=4404, reason="VM not found")
        return

    if vm.state.lower() != "running":
        await websocket.send_json({
            "type": "error",
            "message": f"VM is not running (state: {vm.state}). Start it first.",
        })
        await websocket.close(code=4409, reason="VM is not running")
        return

    if not _try_acquire_session(vm_name):
        await websocket.send_json({
            "type": "error",
            "message": f"Console session limit reached for {vm_name} "
                       f"(max {settings.console_max_sessions}).",
        })
        await websocket.close(code=4429, reason="Console session limit reached")
        return

    try:
        cmd = client.get_console_command(vm_name)

        pid, master_fd = pty.fork()
        if pid == 0:
            # Child process: exec virsh console, never return into app code.
            try:
                os.execvp(cmd[0], cmd)
            except OSError:
                os._exit(127)

        # Parent: set window size from first client message
        _set_winsize(master_fd, rows=24, cols=80)

        await websocket.send_json({
            "type": "connected",
            "message": f"Connected to console of {vm_name}. Press Ctrl+] to exit.",
        })

        async def pty_reader():
            loop = asyncio.get_running_loop()
            try:
                while True:
                    data = await loop.run_in_executor(None, os.read, master_fd, 4096)
                    if not data:
                        break
                    await websocket.send_json(
                        {"type": "output", "data": data.decode("utf-8", errors="replace")}
                    )
            except (OSError, asyncio.CancelledError):
                pass

        async def ws_writer():
            try:
                while True:
                    msg = await websocket.receive_json()
                    if msg.get("type") == "input":
                        os.write(master_fd, msg["data"].encode("utf-8"))
                    elif msg.get("type") == "resize":
                        _set_winsize(master_fd,
                                     rows=msg.get("rows", 24),
                                     cols=msg.get("cols", 80))
            except (WebSocketDisconnect, OSError, asyncio.CancelledError):
                pass

        reader_task = asyncio.create_task(pty_reader())
        writer_task = asyncio.create_task(ws_writer())
        await asyncio.wait(
            [reader_task, writer_task], return_when=asyncio.FIRST_COMPLETED
        )
        for task in (reader_task, writer_task):
            task.cancel()

        try:
            os.close(master_fd)
        except OSError:
            pass
        await run_in_threadpool(_terminate_child, pid)

        try:
            await websocket.close()
        except Exception:
            pass
    finally:
        _release_session(vm_name)


@router.websocket("/ws/console/{vm_name}")
async def console_ws(websocket: WebSocket, vm_name: str):
    await serve_websocket(websocket, vm_name, f"/ws/console/{vm_name}", stream_console)
