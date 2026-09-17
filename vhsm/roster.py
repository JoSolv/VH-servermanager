"""A lasting record of everyone who has played on an instance.

Valheim's console is the only source of player identity a stock server offers,
and it says nothing about anyone who is not currently connected. Moderating
someone who left an hour ago therefore means having written down that they were
here -- so connections, names, deaths and disconnects are folded into a roster
that survives restarts.

Per-player events are kept as a bounded ring: enough to answer "what did this
player do, and when were they last on", without growing without limit on a
server that has been running for months.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .monitor.players import LogEvent
from .util import read_json, write_json

ROSTER_VERSION = 1
#: Events remembered per player. A busy player generates a handful per session.
MAX_EVENTS = 120
#: Players remembered. Beyond this the least recently seen are dropped.
MAX_PLAYERS = 500

#: Human wording for the event kinds worth keeping.
EVENT_LABELS = {
    "connect": "connected",
    "named": "spawned in",
    "died": "died",
    "disconnect": "disconnected",
}


@dataclass(slots=True)
class RosterEntry:
    player_id: str
    names: list[str] = field(default_factory=list)
    first_seen: float = 0.0
    last_seen: float = 0.0
    sessions: int = 0
    playtime: float = 0.0
    events: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""

    @property
    def display_name(self) -> str:
        return self.names[-1] if self.names else ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "player_id": self.player_id,
            "names": self.names,
            "display_name": self.display_name,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "sessions": self.sessions,
            "playtime": round(self.playtime),
            "events": self.events,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RosterEntry":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in payload.items() if k in known})


class Roster:
    """Everyone who has ever joined one instance."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: dict[str, RosterEntry] = {}
        self._dirty = False
        self.load()

    # ------------------------------------------------------------------ #
    def load(self) -> None:
        payload = read_json(self.path, {}) or {}
        self._entries = {}
        for item in payload.get("players", []):
            try:
                entry = RosterEntry.from_dict(item)
            except TypeError:
                continue
            if entry.player_id:
                self._entries[entry.player_id] = entry
        self._dirty = False

    def save(self, force: bool = False) -> None:
        """Write the roster out. Called on a timer, so it is cheap when idle."""
        if not self._dirty and not force:
            return
        # Drop the coldest entries rather than grow without bound.
        entries = sorted(self._entries.values(), key=lambda e: e.last_seen, reverse=True)
        del entries[MAX_PLAYERS:]
        self._entries = {e.player_id: e for e in entries}
        write_json(
            self.path,
            {"version": ROSTER_VERSION, "players": [e.to_dict() for e in entries]},
        )
        self._dirty = False

    # ------------------------------------------------------------------ #
    def _entry(self, player_id: str) -> RosterEntry:
        entry = self._entries.get(player_id)
        if entry is None:
            now = time.time()
            entry = RosterEntry(player_id=player_id, first_seen=now, last_seen=now)
            self._entries[player_id] = entry
        return entry

    def observe(self, event: LogEvent) -> None:
        """Fold one resolved log event into the roster."""
        if not event.player_id:
            return
        entry = self._entry(event.player_id)
        entry.last_seen = event.at

        if event.kind == "connect":
            entry.sessions += 1
        elif event.kind == "named" and event.name:
            if event.name not in entry.names:
                entry.names.append(event.name)
                del entry.names[:-5]          # keep a few recent aliases
            elif entry.names[-1] != event.name:
                entry.names.remove(event.name)
                entry.names.append(event.name)
        elif event.kind == "disconnect" and event.count:
            entry.playtime += float(event.count)

        label = EVENT_LABELS.get(event.kind)
        if label:
            entry.events.append(
                {"at": event.at, "kind": event.kind, "label": label, "name": event.name}
            )
            del entry.events[:-MAX_EVENTS]
        self._dirty = True

    def set_note(self, player_id: str, note: str) -> RosterEntry:
        entry = self._entry(player_id)
        entry.note = note.strip()[:500]
        self._dirty = True
        self.save(force=True)
        return entry

    def forget(self, player_id: str) -> bool:
        if self._entries.pop(player_id, None) is None:
            return False
        self._dirty = True
        self.save(force=True)
        return True

    # ------------------------------------------------------------------ #
    def get(self, player_id: str) -> RosterEntry | None:
        return self._entries.get(player_id)

    def entries(self) -> list[RosterEntry]:
        return sorted(self._entries.values(), key=lambda e: e.last_seen, reverse=True)

    def summary(self, lists: Any = None, online: Iterable[str] = ()) -> list[dict[str, Any]]:
        """The roster with live and moderation state folded in, for the UI."""
        online_ids = set(online)
        rows: list[dict[str, Any]] = []
        for entry in self.entries():
            row = entry.to_dict()
            row["online"] = entry.player_id in online_ids
            # Recent first reads better in a log column.
            row["events"] = list(reversed(row["events"]))[:20]
            if lists is not None:
                row.update(lists.status_for(entry.player_id))
            else:
                row.update({"admin": False, "banned": False, "permitted": False})
            rows.append(row)
        # Online players first, then most recently seen.
        rows.sort(key=lambda r: (not r["online"], -r["last_seen"]))
        return rows
