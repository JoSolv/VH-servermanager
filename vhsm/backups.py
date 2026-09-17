"""World snapshots and rollback.

Snapshots are taken and kept by the manager, in ``<instance>/backups/<stamp>/``
with a small manifest beside the copied world. They are deliberately not
written into ``worlds_local``: a 1.0 world is a *folder*, and a backup folder
sitting next to the live one shows up as another world.

Valheim's own rotating backups are listed alongside them when they can be
recognised, in either save format.

Everything defers to :mod:`vhsm.worlds` for what a world is and which files
belong to it, so a 1.0 folder world and a legacy pair are handled the same way
and a world is always copied whole.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import worlds as worlds_mod
from .util import read_json, write_json

#: Snapshots live under the instance, not under worlds_local.
SNAPSHOT_DIR = "backups"
MANIFEST = "snapshot.json"
PAYLOAD = "payload"

SOURCE_VHSM = "vhsm"
SOURCE_VALHEIM = "valheim"


class BackupError(RuntimeError):
    pass


@dataclass(slots=True)
class Restore:
    """A point the live world can be rolled back to."""

    key: str
    world: str
    kind: str
    source: str
    taken_at: float
    size: int
    #: Directory holding the copied world (its contents are world entries).
    payload: Path
    world_format: str = worlds_mod.FORMAT_FOLDER

    @property
    def label(self) -> str:
        when = datetime.fromtimestamp(self.taken_at).strftime("%Y-%m-%d %H:%M:%S")
        return f"{when} ({self.kind})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "world": self.world,
            "kind": self.kind,
            "source": self.source,
            "taken_at": self.taken_at,
            "label": self.label,
            "size": self.size,
            "format": self.world_format,
        }


# --------------------------------------------------------------------------- #
# listing
# --------------------------------------------------------------------------- #
def snapshot_root(instance_root: Path) -> Path:
    return instance_root / SNAPSHOT_DIR


def _vhsm_snapshots(instance_root: Path, world: str) -> list[Restore]:
    root = snapshot_root(instance_root)
    if not root.is_dir():
        return []
    found: list[Restore] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        payload = read_json(entry / MANIFEST)
        if not isinstance(payload, dict) or payload.get("world") != world:
            continue
        body = entry / PAYLOAD
        if not body.is_dir():
            continue
        found.append(
            Restore(
                key=entry.name,
                world=world,
                kind=str(payload.get("kind") or "manual"),
                source=SOURCE_VHSM,
                taken_at=float(payload.get("taken_at") or entry.stat().st_mtime),
                size=int(payload.get("size") or 0),
                payload=body,
                world_format=str(payload.get("format") or worlds_mod.FORMAT_FOLDER),
            )
        )
    return found


def _valheim_backups(savedir: Path, world: str) -> list[Restore]:
    """Valheim's own rotating backups, in either save format."""
    found: list[Restore] = []
    for directory in worlds_mod.world_dirs(savedir):
        if not directory.is_dir():
            continue

        pairs: dict[tuple[str, str], dict[str, Path]] = {}
        for entry in directory.iterdir():
            stem = entry.name if entry.is_dir() else entry.stem
            match = worlds_mod.RE_BACKUP_NAME.match(stem)
            if not match or match.group("world") != world:
                continue
            kind, stamp = match.group("kind"), match.group("stamp")

            if entry.is_dir():
                size, modified = worlds_mod._tree_size(entry)
                found.append(
                    Restore(
                        key=f"valheim:{entry.name}", world=world, kind=kind,
                        source=SOURCE_VALHEIM, taken_at=modified, size=size,
                        payload=entry, world_format=worlds_mod.FORMAT_FOLDER,
                    )
                )
            elif entry.suffix in (".db", ".fwl"):
                pairs.setdefault((kind, stamp), {})[entry.suffix] = entry

        for (kind, stamp), files in pairs.items():
            db, fwl = files.get(".db"), files.get(".fwl")
            # A .db without its .fwl cannot be loaded, so it is not a restore point.
            if db is None or fwl is None:
                continue
            found.append(
                Restore(
                    key=f"valheim:{db.stem}", world=world, kind=kind,
                    source=SOURCE_VALHEIM,
                    taken_at=db.stat().st_mtime,
                    size=db.stat().st_size + fwl.stat().st_size,
                    payload=db.parent, world_format=worlds_mod.FORMAT_PAIR,
                )
            )
    return found


def list_restores(instance_root: Path, savedir: Path, world: str) -> list[Restore]:
    restores = _vhsm_snapshots(instance_root, world) + _valheim_backups(savedir, world)
    return sorted(restores, key=lambda r: r.taken_at, reverse=True)


def live_world(savedir: Path, world: str) -> dict[str, Any]:
    """Details of the world the server actually loads."""
    found = worlds_mod.find(savedir, world)
    directory = str(worlds_mod.default_world_dir(savedir))
    if found is None:
        others = [w.name for w in worlds_mod.discover(savedir)]
        return {"exists": False, "directory": directory, "other_worlds": others}
    payload = found.to_dict()
    payload.update({"exists": True, "directory": directory, "other_worlds": []})
    return payload


# --------------------------------------------------------------------------- #
# taking and applying
# --------------------------------------------------------------------------- #
def snapshot(instance_root: Path, savedir: Path, world: str, kind: str = "manual") -> Restore:
    found = worlds_mod.find(savedir, world)
    if found is None:
        available = [w.name for w in worlds_mod.discover(savedir)]
        hint = f" Worlds found here: {', '.join(available)}." if available else ""
        raise BackupError(
            f"No world named {world!r} in {worlds_mod.default_world_dir(savedir)}.{hint}"
        )

    root = snapshot_root(instance_root)
    root.mkdir(parents=True, exist_ok=True)
    # Second-resolution stamps collide when two snapshots land in the same
    # second, which a restore does; extend until the name is free.
    base = time.strftime("%Y%m%d%H%M%S")
    stamp, suffix = f"{kind}-{base}", 0
    while (root / stamp).exists():
        suffix += 1
        stamp = f"{kind}-{base}_{suffix}"
        if suffix > 999:
            raise BackupError("could not find a free snapshot name")

    entry = root / stamp
    body = entry / PAYLOAD
    try:
        worlds_mod.copy_world(found, body)
    except OSError as exc:
        shutil.rmtree(entry, ignore_errors=True)
        raise BackupError(f"could not copy the world: {exc}") from exc

    write_json(
        entry / MANIFEST,
        {
            "world": world, "kind": kind, "taken_at": time.time(),
            "size": found.size, "format": found.format,
            "generations": found.generations, "files": found.file_count,
        },
    )
    return Restore(
        key=stamp, world=world, kind=kind, source=SOURCE_VHSM,
        taken_at=time.time(), size=found.size, payload=body, world_format=found.format,
    )


def restore(instance_root: Path, savedir: Path, world: str, key: str) -> Restore:
    """Roll the live world back, snapshotting the current one first."""
    match = next(
        (r for r in list_restores(instance_root, savedir, world) if r.key == key), None
    )
    if match is None:
        raise BackupError(f"no backup {key!r} for world {world!r}")

    # Snapshot first: a rollback that cannot be undone is a trap. This cannot
    # clobber the backup being restored -- snapshots live in their own
    # directory and always pick a free name.
    current = worlds_mod.find(savedir, world)
    if current is not None:
        snapshot(instance_root, savedir, world, kind="manual")

    target_dir = worlds_mod.default_world_dir(savedir)
    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        if current is not None:
            worlds_mod.remove_world(current)

        if match.world_format == worlds_mod.FORMAT_FOLDER:
            source = match.payload
            # A vhsm snapshot wraps the world folder; a Valheim backup folder
            # is the world itself.
            inner = next(
                (p for p in source.iterdir()
                 if p.is_dir() and worlds_mod.looks_like_world_folder(p)),
                None,
            )
            if inner is None and worlds_mod.looks_like_world_folder(source):
                inner = source
            if inner is None:
                raise BackupError("that backup does not contain a readable world")
            destination = target_dir / world
            shutil.rmtree(destination, ignore_errors=True)
            shutil.copytree(inner, destination)
        else:
            source_db = next(match.payload.glob("*.db"), None)
            if source_db is None:
                raise BackupError("that backup has no .db file")
            shutil.copy2(source_db, target_dir / f"{world}.db")
            source_fwl = source_db.with_suffix(".fwl")
            if source_fwl.is_file():
                shutil.copy2(source_fwl, target_dir / f"{world}.fwl")
    except OSError as exc:
        raise BackupError(f"could not restore: {exc}") from exc
    return match


def delete(instance_root: Path, savedir: Path, world: str, key: str) -> None:
    match = next(
        (r for r in list_restores(instance_root, savedir, world) if r.key == key), None
    )
    if match is None:
        raise BackupError(f"no backup {key!r} for world {world!r}")
    if match.source == SOURCE_VHSM:
        shutil.rmtree(match.payload.parent, ignore_errors=True)
        return
    # A Valheim-written backup: remove just that backup's own files.
    if match.world_format == worlds_mod.FORMAT_FOLDER:
        shutil.rmtree(match.payload, ignore_errors=True)
    else:
        name = match.key.split(":", 1)[1]
        for suffix in (".db", ".fwl"):
            (match.payload / f"{name}{suffix}").unlink(missing_ok=True)
