#!/usr/bin/env python3
"""A stand-in for Discord, so the bot can be tested without a network.

The same idea as ``fake_valheim_server.py``: the real thing needs an account,
a token and the internet, none of which belong in a test suite, so this
answers the handful of calls the bot actually makes and records what it was
told. It speaks both halves of the protocol -- the REST API and a gateway
websocket -- which is what makes it possible to check that a slash command
produces the right reply without ever talking to Discord.

Run it standalone to poke at the bot by hand:

    python tools/fake_discord.py --port 9999
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
import time
from typing import Any

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

#: The token the fake accepts. Anything else is rejected the way Discord
#: rejects a bad token, so that path can be tested too.
GOOD_TOKEN = "fake-token-for-tests"
APPLICATION_ID = "111111111111111111"
BOT_USER = {"id": APPLICATION_ID, "username": "vhsm-test", "discriminator": "0"}


class FakeDiscord:
    """Records what the bot says, and lets a test put events in front of it."""

    def __init__(self, port: int = 0) -> None:
        self.port = port or _free_port()
        #: Messages posted, as ``(channel_id, payload)``.
        self.messages: list[tuple[str, dict[str, Any]]] = []
        #: The command set last registered, as ``(guild_id, commands)``.
        self.registered: list[tuple[str, list[dict[str, Any]]]] = []
        #: Interaction responses, as ``(interaction_id, payload)``.
        self.responses: list[tuple[str, dict[str, Any]]] = []
        #: Deferred responses filled in later, as ``(token, payload)``.
        self.followups: list[tuple[str, dict[str, Any]]] = []
        self.identifies = 0
        self.resumes = 0
        self.heartbeats = 0
        #: Set when a connection was refused for a bad token.
        self.rejected = 0
        #: When set, the gateway refuses with 4004 while REST keeps working --
        #: a token revoked under a running bot, which must make it stop rather
        #: than reconnect forever.
        self.gateway_rejects = False

        self._socket: WebSocket | None = None
        self._sequence = 0
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.app = self._build()

    # ------------------------------------------------------------------ #
    @property
    def api_base(self) -> str:
        return f"http://127.0.0.1:{self.port}/api/v10"

    @property
    def gateway_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/gateway"

    @property
    def connected(self) -> bool:
        return self._socket is not None

    def reset(self) -> None:
        """Forget what has been recorded, keeping the connection."""
        self.messages.clear()
        self.registered.clear()
        self.responses.clear()
        self.followups.clear()

    # ------------------------------------------------------------------ #
    def _build(self) -> FastAPI:
        app = FastAPI()
        prefix = "/api/v10"

        def _authorised(request: Request) -> bool:
            return request.headers.get("Authorization") == f"Bot {GOOD_TOKEN}"

        @app.get(prefix + "/gateway/bot")
        async def gateway_bot(request: Request):
            if not _authorised(request):
                return JSONResponse(
                    {"message": "401: Unauthorized", "code": 0}, status_code=401
                )
            return {"url": self.gateway_url, "shards": 1}

        @app.get(prefix + "/users/@me")
        async def users_me(request: Request):
            if not _authorised(request):
                return JSONResponse({"message": "401: Unauthorized"}, status_code=401)
            return BOT_USER

        @app.post(prefix + "/channels/{channel_id}/messages")
        async def create_message(channel_id: str, request: Request):
            if not _authorised(request):
                return JSONResponse({"message": "401: Unauthorized"}, status_code=401)
            # A channel id of nines stands in for one the bot cannot post to,
            # so the "it is not allowed to do that" path has a way to happen.
            if channel_id.startswith("999"):
                return JSONResponse(
                    {"message": "Missing Access", "code": 50001}, status_code=403
                )
            payload = await request.json()
            self.messages.append((channel_id, payload))
            return {"id": str(len(self.messages)), "channel_id": channel_id}

        @app.put(prefix + "/applications/{application_id}/guilds/{guild_id}/commands")
        async def register(application_id: str, guild_id: str, request: Request):
            if not _authorised(request):
                return JSONResponse({"message": "401: Unauthorized"}, status_code=401)
            self.registered.append((guild_id, await request.json()))
            return []

        @app.post(prefix + "/interactions/{interaction_id}/{token}/callback")
        async def callback(interaction_id: str, token: str, request: Request):
            self.responses.append((interaction_id, await request.json()))
            # 204 and no body, as Discord answers a callback. A JSON "null"
            # here is a body, and uvicorn rejects it against the status.
            return Response(status_code=204)

        @app.patch(prefix + "/webhooks/{application_id}/{token}/messages/@original")
        async def followup(application_id: str, token: str, request: Request):
            self.followups.append((token, await request.json()))
            return {"id": "1"}

        # -- the gateway ------------------------------------------------- #
        @app.websocket("/gateway")
        async def gateway(websocket: WebSocket):
            await websocket.accept()
            await websocket.send_json({"op": 10, "d": {"heartbeat_interval": 45000}})
            try:
                while True:
                    message = await websocket.receive_json()
                    await self._handle(websocket, message)
            except (WebSocketDisconnect, RuntimeError, KeyError, ValueError):
                pass
            finally:
                if self._socket is websocket:
                    self._socket = None

        # -- the test's own door ----------------------------------------- #
        @app.post("/_test/dispatch")
        async def dispatch(request: Request):
            """Push a gateway event at the bot, as Discord would."""
            body = await request.json()
            sent = await self.push(str(body.get("t") or ""), body.get("d") or {})
            return {"sent": sent}

        @app.get("/_test/state")
        async def state():
            return {
                "connected": self.connected,
                "identifies": self.identifies,
                "resumes": self.resumes,
                "heartbeats": self.heartbeats,
                "rejected": self.rejected,
                "gateway_rejects": self.gateway_rejects,
                "messages": [
                    {"channel_id": channel, "payload": payload}
                    for channel, payload in self.messages
                ],
                "registered": [
                    {"guild_id": guild, "commands": commands}
                    for guild, commands in self.registered
                ],
                "responses": [
                    {"interaction_id": i, "payload": p} for i, p in self.responses
                ],
                "followups": [{"token": t, "payload": p} for t, p in self.followups],
            }

        @app.post("/_test/reset")
        async def reset():
            self.reset()
            return {"ok": True}

        @app.post("/_test/reject-gateway")
        async def reject_gateway(request: Request):
            body = await request.json()
            self.gateway_rejects = bool(body.get("reject", True))
            return {"gateway_rejects": self.gateway_rejects}

        @app.post("/_test/drop")
        async def drop():
            """Close the gateway socket, so reconnection can be tested."""
            if self._socket is None:
                return {"dropped": False}
            with contextlib.suppress(Exception):
                await self._socket.close(code=1001)
            self._socket = None
            return {"dropped": True}

        return app

    async def _handle(self, websocket: WebSocket, message: dict[str, Any]) -> None:
        op = message.get("op")
        data = message.get("d") or {}

        if op == 2:                                   # IDENTIFY
            if self.gateway_rejects or data.get("token") != GOOD_TOKEN:
                self.rejected += 1
                await websocket.close(code=4004, reason="Authentication failed")
                return
            self.identifies += 1
            self._socket = websocket
            self._sequence += 1
            await websocket.send_json({
                "op": 0, "s": self._sequence, "t": "READY",
                "d": {
                    "session_id": "fake-session",
                    "resume_gateway_url": self.gateway_url,
                    "user": BOT_USER,
                    "application": {"id": APPLICATION_ID},
                },
            })
            return

        if op == 6:                                   # RESUME
            if self.gateway_rejects or data.get("token") != GOOD_TOKEN:
                self.rejected += 1
                await websocket.close(code=4004, reason="Authentication failed")
                return
            self.resumes += 1
            self._socket = websocket
            self._sequence += 1
            await websocket.send_json({
                "op": 0, "s": self._sequence, "t": "RESUMED", "d": {},
            })
            return

        if op == 1:                                   # HEARTBEAT
            self.heartbeats += 1
            await websocket.send_json({"op": 11})
            return

    async def push(self, name: str, data: dict[str, Any]) -> bool:
        """Send one dispatch event over the live socket."""
        if self._socket is None:
            return False
        self._sequence += 1
        try:
            await self._socket.send_json(
                {"op": 0, "s": self._sequence, "t": name, "d": data}
            )
        except Exception:                             # noqa: BLE001
            return False
        return True

    # ------------------------------------------------------------------ #
    # running it
    # ------------------------------------------------------------------ #
    def start(self, timeout: float = 10.0) -> "FakeDiscord":
        """Serve in a background thread and wait until it answers."""
        config = uvicorn.Config(
            self.app, host="127.0.0.1", port=self.port,
            log_level="warning", access_log=False,
        )
        self._server = uvicorn.Server(config)
        # Uvicorn installs signal handlers, which only works on the main
        # thread; this one is not it.
        self._server.install_signal_handlers = lambda: None
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()

        deadline = time.time() + timeout
        while time.time() < deadline:
            if getattr(self._server, "started", False):
                return self
            time.sleep(0.05)
        raise RuntimeError("fake Discord did not start")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server, self._thread = None, None


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    fake = FakeDiscord(args.port).start()
    print(f"fake Discord on {fake.api_base}")
    print(f"gateway        {fake.gateway_url}")
    print(f"token          {GOOD_TOKEN}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        fake.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
