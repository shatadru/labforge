"""Proxy an accepted browser WebSocket to a labforge agent WebSocket.

Used by the control plane in HOST_MODE=remote for the serial and graphical
consoles. The browser-facing origin check and session limits stay on the
control plane; the agent applies its own token check.
"""
import asyncio
import inspect
import logging

import websockets
from fastapi import WebSocket

from app.config import settings

logger = logging.getLogger("labforge.ws")

# websockets renamed extra_headers to additional_headers; support both.
_HEADER_ARG = ("additional_headers"
               if "additional_headers" in inspect.signature(websockets.connect).parameters
               else "extra_headers")


def agent_ws_url(client, path: str) -> str:
    subscribe = getattr(client, "subscribe_ws_url", None)
    if subscribe is None:
        raise RuntimeError("Agent WebSocket proxy requires a remote host client")
    return subscribe(path)


async def _pump(websocket: WebSocket, agent) -> None:
    async def to_agent():
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("text") is not None:
                await agent.send(message["text"])
            elif message.get("bytes") is not None:
                await agent.send(message["bytes"])

    async def to_client():
        async for message in agent:
            if isinstance(message, bytes):
                await websocket.send_bytes(message)
            else:
                await websocket.send_text(message)

    tasks = [asyncio.create_task(to_agent()), asyncio.create_task(to_client())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await websocket.close()
        except Exception:
            pass


async def proxy_websocket(websocket: WebSocket, client, path: str) -> None:
    """Bridge an accepted ``websocket`` to the agent at ``path``."""
    try:
        url = agent_ws_url(client, path)
    except RuntimeError as exc:
        await websocket.close(code=4501, reason=str(exc))
        return

    headers = {}
    if settings.agent_token:
        headers["Authorization"] = f"Bearer {settings.agent_token}"

    try:
        async with websockets.connect(url, **{_HEADER_ARG: headers}, max_size=None) as agent:
            await _pump(websocket, agent)
    except Exception as exc:  # noqa: BLE001 - surface as a clean close
        logger.warning("Agent WebSocket proxy to %s failed: %s", url, exc)
        try:
            await websocket.close(code=4502, reason="Agent unreachable")
        except Exception:
            pass
