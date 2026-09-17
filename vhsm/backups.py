"""World backups and rollback.

Valheim writes its own rotating backups next to the live world, named
``<World>_backup_auto-<timestamp>.db`` with a matching ``.fwl``. Both halves
matter: the ``.db`` holds the world, the ``.fwl`` holds its seed and metadata,
and restoring one without the other produces a world the server will not load.
So a restore point is only offered when the pair is present, and is always
applied as a pair.
"""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

#: Valheim keeps worlds in ``worlds_local`` under the save directory; older
#: installs used ``worlds``. The save directory itself is checked last.
WORLD_DIRS = ("worlds_local", "worlds")

#: ``<world>_backup_auto-20240131-235959.db`` and the manual variant we write.
RE_BACKUP = re.compile(
    r"^(?P<world>.+)_backup_(?P<kind>auto|manual)-(?P<stamp>[0-9_\-]+)\.(?P<ext>db|fwl)$"
)


class BackupError(RuntimeError):
    pass


def world_dir(savedir: Path, world: str) -> Path:
    """Where this world's files live, preferring a directory that has them."""
    for name in WORLD_DIRS:
        candidate = savedir / name
        if candidate.is_dir() and any(candidate.glob(f"{world}*.db")):
            return candidate
    for name in WORLD_DIRS:
        candidate = savedir / name
        if candidate.is_dir():
            return candidate
    return savedir / WORLD_DIRS[0]


@dataclass(slots=True)
class Restore:
    """One restorable snapshot: a matched ``.db`` / ``.fwl`` pair."""

    key: str
    world: str
    kind: str
    stamp: str
    db: Path
    fwl: Path
    taken_at: float
    size: int

    @property
    def label(self) -> str:
        when = datetime.fromtimestamp(self.taken_at).strftime("%Y-%m-%d %H:%M:%S")
        return f"{when} ({self.kind})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "world": self.world,
            "kind": self.kind,
            "stamp": self.stamp,
            "taken_at": self.taken_at,
            "label": self.label,
            "size": self.size,
        }


def _stamp_to_time(stamp: str, fallback: float) -> float:
    digits = re.sub(r"[^0-9]", "", stamp)
    for fmt in ("%Y%m%d%H%M%S", "%Y%m%d%H%M", "%Y%m%d"):
        if len(digits) == len(datetime.now().strftime(fmt)):
            try:
                return datetime.strptime(digits, fmt).timestamp()
            except ValueError:
                break
    return fallback


def list_restores(savedir: Path, world: str) -> list[Restore]:
    """Every snapshot that can be rolled back to, newest first."""
    directory = world_dir(savedir, world)
    if not directory.is_dir():
        return []

    pairs: dict[tuple[str, str], dict[str, Path]] = {}
    for path in directory.iterdir():
        if not path.is_file():
            continue
        match = RE_BACKUP.match(path.name)
        if not match or match.group("world") != world:
            continue
        key = (match.group("kind"), match.group("stamp"))
        pairs.setdefault(key, {})[match.group("ext")] = path

    restores: list[Restore] = []
    for (kind, stamp), files in pairs.items():
        db, fwl = files.get("db"), files.get("fwl")
        # A .db without its .fwl cannot be loaded, so it is not a restore point.
        if db is None or fwl is None:
            continue
        restores.append(
            Restore(
                key=f"{kind}-{stamp}",
                world=world,
                kind=kind,
                stamp=stamp,
                db=db,
                fwl=fwl,
                taken_at=_stamp_to_time(stamp, db.stat().st_mtime),
                size=db.stat().st_size + fwl.stat().st_size,
            )
        )
    return sorted(restores, key=lambda r: r.taken_at, reverse=True)


def live_world(savedir: Path, world: str) -> dict[str, Any]:
    """Details of the world the server actually loads."""
    directory = world_dir(savedir, world)
    db, fwl = directory / f"{world}.db", directory / f"{world}.fwl"
    if not db.is_file():
        return {"exists": False, "directory": str(directory)}
    return {
        "exists": True,
        "directory": str(directory),
        "size": db.stat().st_size + (fwl.stat().st_size if fwl.is_file() else 0),
        "modified": db.stat().st_mtime,
        "has_metadata": fwl.is_file(),
    }


def snapshot(savedir: Path, world: str, kind: str = "manual") -> Restore | None:
    """Copy the live world aside as a new restore point.

    Taken before a rollback so the rollback itself can be undone.
    """
    directory = world_dir(savedir, world)
    db, fwl = directory / f"{world}.db", directory / f"{world}.fwl"
    if not db.is_file():
        return None

    # Second-resolution stamps collide when two snapshots land in the same
    # second -- which a restore does, since it snapshots before copying. A
    # collision would overwrite an existing restore point with the live world,
    # destroying the very state the user is rolling back to, so the stamp is
    # extended until it names files that do not exist yet.
    base = time.strftime("%Y%m%d%H%M%S")
    stamp, suffix = base, 0
    while (directory / f"{world}_backup_{kind}-{stamp}.db").exists() or (
        directory / f"{world}_backup_{kind}-{stamp}.fwl"
    ).exists():
        suffix += 1
        stamp = f"{base}_{suffix}"
        if suffix > 999:
            raise BackupError("could not find a free backup name")

    target_db = directory / f"{world}_backup_{kind}-{stamp}.db"
    target_fwl = directory / f"{world}_backup_{kind}-{stamp}.fwl"
    shutil.copy2(db, target_db)
    if fwl.is_file():
        shutil.copy2(fwl, target_fwl)
    else:
        # Keep the pair complete so the snapshot is offered as a restore point.
        target_fwl.write_bytes(b"")

    return Restore(
        key=f"{kind}-{stamp}", world=world, kind=kind, stamp=stamp,
        db=target_db, fwl=target_fwl, taken_at=time.time(),
        size=target_db.stat().st_size + target_fwl.stat().st_size,
    )


def restore(savedir: Path, world: str, key: str) -> Restore:
    """Roll the live world back to a snapshot, keeping the current one first."""
    match = next((r for r in list_restores(savedir, world) if r.key == key), None)
    if match is None:
        raise BackupError(f"no backup {key!r} for world {world!r}")
    if not match.db.is_file() or not match.fwl.is_file():
        raise BackupError("that backup is missing one of its two files")

    directory = world_dir(savedir, world)
    # Snapshot first: a rollback that cannot be undone is a trap. This cannot
    # clobber the backup being restored, because snapshot() picks a free name.
    snapshot(savedir, world, kind="manual")

    try:
        shutil.copy2(match.db, directory / f"{world}.db")
        shutil.copy2(match.fwl, directory / f"{world}.fwl")
    except OSError as exc:
        raise BackupError(f"could not restore: {exc}") from exc
    return match


def delete(savedir: Path, world: str, key: str) -> None:
    match = next((r for r in list_restores(savedir, world) if r.key == key), None)
    if match is None:
        raise BackupError(f"no backup {key!r} for world {world!r}")
    match.db.unlink(missing_ok=True)
    match.fwl.unlink(missing_ok=True)
