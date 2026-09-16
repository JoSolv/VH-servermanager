"""Minimal Steam A2S client.

Valheim exposes a standard Source query socket on ``game port + 1``. Asking it
directly is the authoritative way to read the live player count -- Valheim has
no RCON, and the console log only tells us about state *changes*.

Implemented inline rather than pulled from a dependency: the two request types
we need are a few dozen lines and this avoids a hard requirement on a package
that must be reachable at install time.
"""

from __future__ import annotations

import asyncio
import socket
import struct
from dataclasses import dataclass, field

WHOLE_PACKET = b"\xff\xff\xff\xff"
A2S_INFO_REQUEST = WHOLE_PACKET + b"\x54" + b"Source Engine Query\x00"
A2S_PLAYER_HEADER = WHOLE_PACKET + b"\x55"
CHALLENGE_RESPONSE = b"A"


class A2SError(Exception):
    """The server did not answer, or answered with something unparseable."""


@dataclass(slots=True)
class ServerInfo:
    name: str = ""
    map_name: str = ""
    game: str = ""
    players: int = 0
    max_players: int = 0
    version: str = ""
    visibility: int = 0
    player_names: list[str] = field(default_factory=list)
    #: Seconds each player has been connected, parallel to ``player_names``.
    player_durations: list[float] = field(default_factory=list)


class _Reader:
    """Cursor over a response packet."""

    def __init__(self, payload: bytes) -> None:
        self._data = payload
        self._pos = 0

    def byte(self) -> int:
        value = self._data[self._pos]
        self._pos += 1
        return value

    def short(self) -> int:
        value = struct.unpack_from("<H", self._data, self._pos)[0]
        self._pos += 2
        return value

    def long(self) -> int:
        value = struct.unpack_from("<i", self._data, self._pos)[0]
        self._pos += 4
        return value

    def float(self) -> float:
        value = struct.unpack_from("<f", self._data, self._pos)[0]
        self._pos += 4
        return value

    def string(self) -> str:
        end = self._data.index(b"\x00", self._pos)
        value = self._data[self._pos:end].decode("utf-8", errors="replace")
        self._pos = end + 1
        return value

    @property
    def remaining(self) -> int:
        return len(self._data) - self._pos


def _exchange(host: str, port: int, request: bytes, timeout: float) -> bytes:
    """Send one datagram and return the reply, transparently answering challenges."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(request, (host, port))
        for _ in range(3):
            try:
                reply = sock.recv(4096)
            except socket.timeout as exc:
                raise A2SError(f"no response from {host}:{port}") from exc
            if not reply.startswith(WHOLE_PACKET):
                raise A2SError("malformed packet")
            if reply[4:5] == CHALLENGE_RESPONSE:
                # Re-send with the challenge appended (or substituted, for
                # A2S_INFO which carries its payload first).
                challenge = reply[5:9]
                if request.startswith(A2S_INFO_REQUEST):
                    sock.sendto(A2S_INFO_REQUEST + challenge, (host, port))
                else:
                    sock.sendto(request[:5] + challenge, (host, port))
                continue
            return reply
        raise A2SError("challenge loop did not converge")


def _query_blocking(host: str, port: int, timeout: float) -> ServerInfo:
    reply = _exchange(host, port, A2S_INFO_REQUEST, timeout)
    reader = _Reader(reply[4:])
    if reader.byte() != 0x49:  # 'I'
        raise A2SError("unexpected response type for A2S_INFO")

    info = ServerInfo()
    reader.byte()                      # protocol version
    info.name = reader.string()
    info.map_name = reader.string()
    reader.string()                    # folder
    info.game = reader.string()
    reader.short()                     # app id
    info.players = reader.byte()
    info.max_players = reader.byte()
    reader.byte()                      # bots
    reader.byte()                      # server type
    reader.byte()                      # environment
    info.visibility = reader.byte()
    reader.byte()                      # VAC
    info.version = reader.string()

    # A2S_PLAYERS is best-effort: Valheim often returns an empty list because
    # players are not registered as Source-style clients.
    try:
        reply = _exchange(host, port, A2S_PLAYER_HEADER + b"\xff\xff\xff\xff", timeout)
        reader = _Reader(reply[4:])
        if reader.byte() == 0x44:  # 'D'
            for _ in range(reader.byte()):
                reader.byte()          # index
                info.player_names.append(reader.string())
                reader.long()          # score
                info.player_durations.append(reader.float())
    except (A2SError, IndexError, struct.error, ValueError):
        pass
    return info


async def query(host: str, port: int, timeout: float = 2.0) -> ServerInfo:
    """Query a server, raising :class:`A2SError` if it does not answer."""
    try:
        return await asyncio.to_thread(_query_blocking, host, port, timeout)
    except (IndexError, struct.error, ValueError) as exc:
        raise A2SError(f"could not parse response: {exc}") from exc
