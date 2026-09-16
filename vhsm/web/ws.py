"""Websocket endpoints: live metrics and live console output."""

from __future__ import annotations

import asyncio
import contextlib
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..manager import ManagerError

router = APIRouter()


@router.websocket("/ws/metrics")
async def metrics_socket(websocket: WebSocket) -> None:
    """Push a full snapshot of every instance on each sampling tick."""
    await websocket.accept()
    manager = websocket.app.state.manager
    queue = manager.hub.subscribe()
    try:
        # Send one immediately so the page is populated before the next tick.
        await websocket.send_text(json.dumps(await manager.sample_once(), default=str))
        while True:
            await websocket.send_text(await queue.get())
    except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
        pass
    finally:
        manager.hub.unsubscribe(queue)


@router.websocket("/ws/console/{instance_id}")
async def console_socket(websocket: WebSocket, instance_id: str) -> None:
    """Stream one instance's console output, starting with recent history."""
    await websocket.accept()
    manager = websocket.app.state.manager
    try:
        record = manager.get(instance_id)
    except ManagerError:
        await websocket.close(code=4004)
        return

    supervisor = record.supervisor
    queue = supervisor.subscribe()
    try:
        for line in supervisor.recent_logs(300):
            await websocket.send_text(line)
        while True:
            await websocket.send_text(await queue.get())
    except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
        pass
    finally:
        supervisor.unsubscribe(queue)
        with contextlib.suppress(Exception):
            await websocket.close()
