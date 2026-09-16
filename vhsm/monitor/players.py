"""Tracking who is connected.

Two independent signals are combined:

* the Steam A2S query socket gives an authoritative *count*;
* the console log gives *names*, which A2S does not reliably report for
  Valheim.

Either can be missing -- a server that is still booting answers neither -- so
the tracker degrades to whichever it has.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

# Console lines emitted by the dedicated server, in connection order.
RE_CONNECT = re.compile(r"Got connection SteamID (\S+)")
RE_HANDSHAKE = re.compile(r"Got handshake from client (\S+)")
RE_CHARACTER = re.compile(r"Got character ZDOID from (.+?) : (-?\d+):(-?\d+)")
RE_DISCONNECT = re.compile(r"Closing socket (\S+)")
RE_CONNECTIONS = re.compile(r"Connections (\d+) ZDOS")


@dataclass(slots=True)
class Player:
    session_id: str
    name: str = ""
    joined_at: float = field(default_factory=time.time)
    #: Where we learned about this player: the console log (which carries the
    #: platform id, so moderation is possible) or the Steam query (name only).
    source: str = "log"
    #: Connection time as reported by A2S, when the server reports one.
    query_duration: float | None = None
    #: Character id from the ZDOID line; present once the player is in-world.
    character_id: str = ""

    @property
    def playtime(self) -> float:
        return time.time() - self.joined_at

    @property
    def player_id(self) -> str:
        """The id Valheim's admin/ban lists key on, when we know it."""
        return self.session_id if self.source == "log" else ""

    @property
    def platform(self) -> str:
        """Best-effort platform label for the id we hold."""
        if self.source != "log":
            return "unknown"
        if self.session_id.isdigit() and self.session_id.startswith("7656"):
            return "Steam"
        if "_" in self.session_id:
            return self.session_id.split("_", 1)[0]
        return "other"

    def to_dict(self, lists: Any = None) -> dict[str, Any]:
        payload = {
            "session_id": self.session_id,
            "player_id": self.player_id,
            "platform": self.platform,
            "name": self.name or "connecting...",
            "in_world": bool(self.character_id),
            "joined_at": self.joined_at,
            "playtime": round(self.playtime),
            "connected_for": round(self.query_duration) if self.query_duration else None,
            # Valheim exposes no per-player latency: there is no RCON, the
            # console never logs it, and A2S_PLAYERS carries only a connection
            # duration. Reporting a ping would mean inventing one.
            "ping": None,
            "can_moderate": bool(self.player_id),
        }
        if lists is not None and self.player_id:
            payload.update(lists.status_for(self.player_id))
        else:
            payload.update({"admin": False, "banned": False, "permitted": False})
        return payload


class PlayerTracker:
    """Per-instance view of connected players."""

    def __init__(self) -> None:
        self._players: dict[str, Player] = {}
        self._pending: list[str] = []   # connected, awaiting a character name
        self.query_count: int | None = None
        self.max_players: int = 0

    def reset(self) -> None:
        self._players.clear()
        self._pending.clear()
        self.query_count = None

    # ------------------------------------------------------------------ #
    # log-driven updates
    # ------------------------------------------------------------------ #
    def observe_log(self, line: str) -> None:
        match = RE_CONNECT.search(line) or RE_HANDSHAKE.search(line)
        if match:
            session_id = match.group(1)
            if session_id not in self._players:
                self._players[session_id] = Player(session_id=session_id)
                self._pending.append(session_id)
            return

        match = RE_CHARACTER.search(line)
        if match:
            name = match.group(1).strip()
            character_id = match.group(2)
            # A ZDOID line names the character; attach it to the most recent
            # unnamed connection. Re-spawns re-use an already named session.
            for player in self._players.values():
                if player.name == name:
                    player.character_id = character_id
                    return
            while self._pending:
                session_id = self._pending.pop(0)
                player = self._players.get(session_id)
                if player is not None and not player.name:
                    player.name = name
                    player.character_id = character_id
                    return
            return

        match = RE_DISCONNECT.search(line)
        if match:
            session_id = match.group(1)
            self._players.pop(session_id, None)
            if session_id in self._pending:
                self._pending.remove(session_id)
            return

        match = RE_CONNECTIONS.search(line)
        if match:
            self.query_count = int(match.group(1))

    # ------------------------------------------------------------------ #
    # query-driven updates
    # ------------------------------------------------------------------ #
    def observe_query(
        self, count: int, max_players: int, names: list[str], durations: list[float] | None = None
    ) -> None:
        self.query_count = count
        self.max_players = max_players
        durations = durations or []
        for index, name in enumerate(names):
            duration = durations[index] if index < len(durations) else None
            existing = next((p for p in self._players.values() if p.name == name), None)
            if existing is not None:
                existing.query_duration = duration
                continue
            if name:
                key = f"a2s:{index}:{name}"
                self._players.setdefault(
                    key,
                    Player(session_id=key, name=name, source="query", query_duration=duration),
                )

    # ------------------------------------------------------------------ #
    @property
    def players(self) -> list[Player]:
        return sorted(self._players.values(), key=lambda p: p.joined_at)

    @property
    def count(self) -> int:
        """Prefer the authoritative query count, fall back to log tracking."""
        return self.query_count if self.query_count is not None else len(self._players)

    def to_dict(self, lists: Any = None) -> dict[str, Any]:
        return {
            "count": self.count,
            "max": self.max_players,
            "players": [p.to_dict(lists) for p in self.players],
        }
