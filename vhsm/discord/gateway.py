"""The websocket half: receiving slash commands without opening a port.

Discord can deliver an interaction two ways. One is an HTTP endpoint it POSTs
to, which needs a public HTTPS URL on this host -- the one thing the manager
binding to localhost exists to avoid. The other is the gateway: a websocket
*we* open, over which interactions arrive like any other event. That is what
this is, and it is why the bot works behind NAT with no port forwarded and no
certificate.

Only the connection lifecycle lives here -- connect, identify, heartbeat,
resume, back off -- deliberately separate from what the events mean. The
protocol is documented at https://discord.com/developers/docs/topics/gateway.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from typing import Any, Awaitable, Callable

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, WebSocketException

log = logging.getLogger("vhsm.discord.gateway")

# Gateway opcodes.
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

#: Slash commands need no intents at all. Prefix commands would need Message
#: Content, which is privileged and can be switched off underneath a working
#: install; this way there is nothing to approve and nothing to lose.
INTENTS = 0

#: Close codes Discord documents as final. Retrying these is not "resilience",
#: it is hammering an API that has already given its answer -- and 4004 in
#: particular means the token is wrong, which no amount of waiting fixes.
FATAL_CLOSE_CODES: dict[int, str] = {
    4004: (
        "Discord rejected the bot token. Check it in the Developer Portal "
        "under Bot, and use Reset Token if you no longer have it."
    ),
    4010: "Discord rejected the shard configuration.",
    4011: "This bot is in too many guilds to run unsharded.",
    4012: "This gateway API version is no longer supported; vhsm needs an update.",
    4013: "The gateway rejected the intents vhsm asked for.",
    4014: (
        "The gateway refused a privileged intent. Enable it in the Developer "
        "Portal, or update vhsm."
    ),
}

#: Reconnect backoff, in seconds, with jitter on top.
BACKOFF_START = 2.0
BACKOFF_MAX = 60.0
#: How long to wait for HELLO before deciding the socket is no good.
HELLO_TIMEOUT = 20.0


class GatewayClient:
    """One gateway connection, kept up for as long as :meth:`run` is awaited.

    Two callbacks: *on_ready* when a fresh session is established (carrying
    the READY payload, which is where the application id comes from), and
    *on_dispatch* for every event. Both are awaited, and an exception in
    either is logged rather than allowed to take the connection down -- a
    command handler that raises must not cost us the socket.
    """

    def __init__(
        self,
        token: str,
        *,
        gateway_url: str,
        on_ready: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        on_dispatch: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self._token = token
        self._url = gateway_url
        self._on_ready = on_ready
        self._on_dispatch = on_dispatch

        #: ``connecting`` | ``connected`` | ``reconnecting`` | ``stopped``
        self.state: str = "stopped"
        self.last_error: str = ""
        self.connected_at: float = 0.0
        self.session_id: str = ""
        self.user: dict[str, Any] = {}
        self.application_id: str = ""
        #: How many times the socket has come up, so the GUI can distinguish
        #: "connected" from "reconnecting every ten seconds".
        self.connects: int = 0

        self._sequence: int | None = None
        self._resume_url: str = ""
        self._socket: Any = None
        self._heartbeat: asyncio.Task[None] | None = None
        self._acked = True
        self._interval = 41.25
        self._stopping = False
        #: Whether the current attempt got as far as a working session, which
        #: is what decides whether the reconnect delay starts over.
        self._established = False

    # ------------------------------------------------------------------ #
    @property
    def connected(self) -> bool:
        return self.state == "connected"

    async def run(self) -> None:
        """Keep a session up until cancelled, or until something is final.

        Every recoverable failure comes back here and is retried with a
        growing delay. A close code in :data:`FATAL_CLOSE_CODES` instead sets
        :attr:`last_error` and returns, leaving the GUI something to show
        rather than a socket that retries a question Discord has already
        answered -- 4004 above all, which means the token is wrong.
        """
        backoff = BACKOFF_START
        while not self._stopping:
            self.state = "connecting" if not self.connects else "reconnecting"
            self._established = False
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except (ConnectionClosed, WebSocketException, OSError) as exc:
                fatal = _fatal_reason(exc)
                if fatal:
                    self.state = "stopped"
                    self.last_error = fatal
                    log.error("discord gateway stopped: %s", fatal)
                    return
                self.last_error = _describe(exc)
                log.warning("discord gateway lost (%s); retrying", self.last_error)
            except Exception as exc:  # noqa: BLE001 - must not kill the loop
                self.last_error = str(exc)
                log.exception("discord gateway failed unexpectedly")
            finally:
                await self._stop_heartbeat()

            if self._stopping:
                break
            # A session that actually came up earns a fresh budget. Growing
            # the delay off the *number* of drops would mean a bot that had
            # been up for a week reconnecting an hour later just because it
            # had been up long enough to have dropped a few times.
            if self._established:
                backoff = BACKOFF_START
            self.state = "reconnecting"
            # Jitter, so a manager restarting several things at once does not
            # reconnect them all on the same tick.
            await asyncio.sleep(backoff + random.uniform(0, backoff / 2))
            backoff = min(backoff * 2, BACKOFF_MAX)
        self.state = "stopped"

    async def stop(self) -> None:
        self._stopping = True
        await self._stop_heartbeat()
        if self._socket is not None:
            with contextlib.suppress(Exception):
                await self._socket.close()
        self.state = "stopped"

    # ------------------------------------------------------------------ #
    async def _session(self) -> None:
        """One connection, from HELLO until the socket goes away."""
        resuming = bool(self.session_id and self._resume_url)
        url = _versioned(self._resume_url if resuming else self._url)

        # ping_interval=None: the gateway has its own heartbeat protocol and
        # that is the one Discord judges liveness by, so a second, lower-level
        # keepalive would only add a way for the two to disagree.
        async with connect(url, ping_interval=None, open_timeout=HELLO_TIMEOUT) as socket:
            self._socket = socket
            hello = await asyncio.wait_for(self._receive(), HELLO_TIMEOUT)
            if hello.get("op") != OP_HELLO:
                raise WebSocketException(f"expected HELLO, got op {hello.get('op')}")
            self._interval = float(hello["d"]["heartbeat_interval"]) / 1000.0
            self._acked = True
            self._heartbeat = asyncio.create_task(self._heartbeat_loop())

            if resuming:
                log.info("discord: resuming session %s", self.session_id)
                await self._send({
                    "op": OP_RESUME,
                    "d": {
                        "token": self._token,
                        "session_id": self.session_id,
                        "seq": self._sequence,
                    },
                })
            else:
                await self._send({
                    "op": OP_IDENTIFY,
                    "d": {
                        "token": self._token,
                        "intents": INTENTS,
                        "properties": {
                            "os": "linux",
                            "browser": "vhsm",
                            "device": "vhsm",
                        },
                    },
                })

            while not self._stopping:
                await self._handle(await self._receive())

    async def _receive(self) -> dict[str, Any]:
        raw = await self._socket.recv()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise WebSocketException(f"gateway sent something that is not JSON: {exc}")
        return payload if isinstance(payload, dict) else {}

    async def _send(self, payload: dict[str, Any]) -> None:
        await self._socket.send(json.dumps(payload))

    async def _handle(self, payload: dict[str, Any]) -> None:
        op = payload.get("op")

        if op == OP_DISPATCH:
            if payload.get("s") is not None:
                self._sequence = int(payload["s"])
            name = str(payload.get("t") or "")
            data = payload.get("d") or {}
            if name == "READY":
                await self._ready(data)
            elif name == "RESUMED":
                self.state = "connected"
                self._established = True
                log.info("discord: session resumed")
            if self._on_dispatch is not None and isinstance(data, dict):
                try:
                    await self._on_dispatch(name, data)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - a handler must not cost us the socket
                    log.exception("discord: handler for %s failed", name)
            return

        if op == OP_HEARTBEAT:
            await self._beat()                 # the gateway asked for one now
            return

        if op == OP_HEARTBEAT_ACK:
            self._acked = True
            return

        if op == OP_RECONNECT:
            log.info("discord: gateway asked us to reconnect")
            raise WebSocketException("gateway asked for a reconnect")

        if op == OP_INVALID_SESSION:
            # d is True when the session may still be resumed. When it is
            # False the session is gone and a fresh IDENTIFY is required --
            # after a pause, because Discord rate limits identifying.
            if not payload.get("d"):
                log.info("discord: session invalidated, identifying afresh")
                self.session_id, self._resume_url = "", ""
                await asyncio.sleep(random.uniform(1.0, 5.0))
            raise WebSocketException("session invalidated")

    async def _ready(self, data: dict[str, Any]) -> None:
        self.session_id = str(data.get("session_id") or "")
        self._resume_url = str(data.get("resume_gateway_url") or "")
        self.user = data.get("user") or {}
        application = data.get("application") or {}
        self.application_id = str(application.get("id") or "")
        self.state = "connected"
        self._established = True
        self.connected_at = time.time()
        self.connects += 1
        self.last_error = ""
        log.info(
            "discord: connected as %s (application %s)",
            self.user.get("username") or "?", self.application_id or "?",
        )
        if self._on_ready is not None:
            try:
                await self._on_ready(data)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("discord: on_ready failed")

    # -- heartbeat ------------------------------------------------------- #
    async def _beat(self) -> None:
        await self._send({"op": OP_HEARTBEAT, "d": self._sequence})

    async def _heartbeat_loop(self) -> None:
        """Beat forever, and give up on a socket that stops acknowledging.

        A gateway connection can stay open at the TCP level long after it has
        stopped carrying events ("a zombie"), and the only thing that reveals
        it is a missing ACK. Discord's own guidance is to drop the socket and
        resume rather than keep beating into it.
        """
        # The first beat is offset by a random fraction of the interval, as
        # Discord asks, so every bot in the world does not beat in lockstep.
        await asyncio.sleep(self._interval * random.random())
        while True:
            if not self._acked:
                log.warning("discord: no heartbeat ack, dropping the socket")
                with contextlib.suppress(Exception):
                    await self._socket.close(code=1002, reason="no heartbeat ack")
                return
            self._acked = False
            try:
                await self._beat()
            except (ConnectionClosed, WebSocketException, OSError):
                return
            await asyncio.sleep(self._interval)

    async def _stop_heartbeat(self) -> None:
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._heartbeat
            self._heartbeat = None


def _versioned(url: str) -> str:
    """Add the API version and encoding the gateway expects."""
    base = url.split("?", 1)[0].rstrip("/")
    return f"{base}?v=10&encoding=json"


def _fatal_reason(exc: BaseException) -> str:
    """The reason this close code is final, or ``""`` if it is worth retrying."""
    return FATAL_CLOSE_CODES.get(getattr(exc, "code", None) or 0, "")


def _describe(exc: BaseException) -> str:
    """A closed socket in words."""
    code = getattr(exc, "code", None)
    if code:
        reason = getattr(exc, "reason", "") or ""
        return f"connection closed ({code}{': ' + reason if reason else ''})"
    return str(exc) or exc.__class__.__name__
