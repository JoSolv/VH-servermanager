"""Admin, banned and permitted player lists.

Valheim keeps three plain-text lists in the save directory and re-reads them
while running, so edits take effect without a restart -- adding an id to the
banned list disconnects that player within seconds. That reload is the only
moderation hook a stock dedicated server exposes: there is no RCON and the
server ignores stdin, so "kick" is implemented as a short ban that is lifted
automatically (see :class:`TempBans`).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .util import read_json, write_json

#: Steam ids are numeric; crossplay ids carry a platform prefix such as
#: ``Steam_7656...`` or an XboxLive/PlayFab id. Accept both shapes, reject
#: anything that could inject a comment or a second entry.
ID_PATTERN = re.compile(r"^[A-Za-z0-9_]{3,64}$")

#: How long a "kick" ban stays in place before it is lifted.
KICK_BAN_SECONDS = 20.0


class PlayerListError(ValueError):
    pass


def normalise_id(value: str) -> str:
    value = (value or "").strip()
    if not ID_PATTERN.match(value):
        raise PlayerListError(
            f"{value!r} is not a valid player id (letters, digits and underscores only)."
        )
    return value


@dataclass(slots=True)
class PlayerList:
    """One of Valheim's three list files."""

    key: str
    path: Path
    header: str

    def read(self) -> list[str]:
        try:
            raw = self.path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return []
        entries: list[str] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            # Valheim only reads the leading token; ignore trailing notes.
            token = line.split()[0]
            if ID_PATTERN.match(token) and token not in entries:
                entries.append(token)
        return entries

    def write(self, entries: Iterable[str]) -> None:
        unique: list[str] = []
        for entry in entries:
            entry = normalise_id(entry)
            if entry not in unique:
                unique.append(entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(unique)
        self.path.write_text(f"{self.header}\n{body}\n" if body else f"{self.header}\n",
                             encoding="utf-8")

    def contains(self, player_id: str) -> bool:
        return normalise_id(player_id) in self.read()

    def add(self, player_id: str) -> bool:
        player_id = normalise_id(player_id)
        entries = self.read()
        if player_id in entries:
            return False
        entries.append(player_id)
        self.write(entries)
        return True

    def remove(self, player_id: str) -> bool:
        player_id = normalise_id(player_id)
        entries = self.read()
        if player_id not in entries:
            return False
        self.write([e for e in entries if e != player_id])
        return True


class TempBans:
    """Bans that expire, so a player can be kicked without a permanent ban.

    Persisted to disk: if the manager restarts while a kick is in flight the
    ban is still lifted on the next reconcile, rather than stranding the
    player on the banned list forever.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict[str, float]:
        payload = read_json(self.path, {}) or {}
        if not isinstance(payload, dict):
            return {}
        return {str(k): float(v) for k, v in payload.items() if isinstance(v, (int, float))}

    def _save(self, entries: dict[str, float]) -> None:
        if entries:
            write_json(self.path, entries)
        else:
            self.path.unlink(missing_ok=True)

    def schedule(self, player_id: str, seconds: float = KICK_BAN_SECONDS) -> float:
        entries = self._load()
        expires = time.time() + seconds
        entries[normalise_id(player_id)] = expires
        self._save(entries)
        return expires

    def cancel(self, player_id: str) -> None:
        entries = self._load()
        if entries.pop(normalise_id(player_id), None) is not None:
            self._save(entries)

    def pending(self) -> dict[str, float]:
        return self._load()

    def due(self) -> list[str]:
        now = time.time()
        return [pid for pid, expires in self._load().items() if expires <= now]


class PlayerLists:
    """The three lists for one instance, plus kick bookkeeping."""

    def __init__(self, savedir: Path, state_dir: Path) -> None:
        self.admins = PlayerList(
            "admins", savedir / "adminlist.txt", "// List admin players ID, one per line."
        )
        self.banned = PlayerList(
            "banned", savedir / "bannedlist.txt", "// List banned players ID, one per line."
        )
        self.permitted = PlayerList(
            "permitted", savedir / "permittedlist.txt",
            "// List permitted players ID, one per line.",
        )
        self.temp_bans = TempBans(state_dir / "tempbans.json")

    def by_key(self, key: str) -> PlayerList:
        lists = {"admins": self.admins, "banned": self.banned, "permitted": self.permitted}
        if key not in lists:
            raise PlayerListError(f"unknown list {key!r}")
        return lists[key]

    def kick(self, player_id: str, seconds: float = KICK_BAN_SECONDS) -> float:
        """Disconnect a player by banning them briefly.

        A stock server offers no other way to remove someone who is online.
        """
        player_id = normalise_id(player_id)
        if self.banned.contains(player_id):
            raise PlayerListError("That player is already banned.")
        self.banned.add(player_id)
        return self.temp_bans.schedule(player_id, seconds)

    def reconcile_temp_bans(self) -> list[str]:
        """Lift any kick bans that have expired. Safe to call repeatedly."""
        lifted: list[str] = []
        for player_id in self.temp_bans.due():
            self.banned.remove(player_id)
            self.temp_bans.cancel(player_id)
            lifted.append(player_id)
        return lifted

    def status_for(self, player_id: str) -> dict[str, bool]:
        if not player_id or not ID_PATTERN.match(player_id):
            return {"admin": False, "banned": False, "permitted": False}
        return {
            "admin": player_id in self.admins.read(),
            "banned": player_id in self.banned.read(),
            "permitted": player_id in self.permitted.read(),
        }

    def summary(self) -> dict[str, Any]:
        pending = self.temp_bans.pending()
        return {
            "admins": self.admins.read(),
            "banned": self.banned.read(),
            "permitted": self.permitted.read(),
            "temp_bans": {k: round(v - time.time()) for k, v in pending.items()},
        }
