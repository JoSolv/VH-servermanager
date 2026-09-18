"""Locating and moving Valheim worlds, in both save formats.

Valheim 1.0 replaced the old ``<World>.db`` + ``<World>.fwl`` pair with a
**folder** named after the world, holding ``_main.<N>.db2``, ``_main.<N>.fwl2``,
``_main.<N>.chunks``, an ``_main.<N>.ok`` completion marker and a pile of
``.chunk`` terrain files. ``N`` is a save counter, and several generations sit
side by side in the same folder.

Two consequences drive everything here:

* Looking for ``<World>.db`` finds nothing on a 1.0 server, so anything built
  on that assumption silently reports "no world".
* A partial copy of a 1.0 world is not a smaller world, it is a broken one.
  Worlds are therefore always copied whole, never file by file.

Both formats are recognised, because a server upgraded from an older version
can still be carrying a legacy world.
"""

from __future__ import annotations

import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Directory names Valheim keeps worlds in, newest convention first.
WORLD_DIRS = ("worlds_local", "worlds")

FORMAT_FOLDER = "folder"   # Valheim 1.0 and later
FORMAT_PAIR = "pair"       # legacy .db + .fwl

#: ``_main.3.db2`` -> generation 3.
RE_GENERATION = re.compile(r"^_main\.(?P<n>\d+)\.(?P<ext>db2|fwl2|chunks|ok)$")
#: Valheim's own rotating backups, in either format.
RE_BACKUP_NAME = re.compile(r"^(?P<world>.+?)_backup_(?P<kind>auto|manual)-(?P<stamp>[0-9_\-]+)$")

#: Extensions that make a directory look like a 1.0 world.
FOLDER_MARKERS = (".db2", ".fwl2", ".chunk", ".chunks")


class WorldError(RuntimeError):
    pass


@dataclass(slots=True)
class World:
    """One world on disk, whatever format it is stored in."""

    name: str
    format: str
    #: The world folder (1.0) or the directory holding the pair (legacy).
    root: Path
    #: Every path belonging to this world. For 1.0 this is the folder itself.
    paths: list[Path] = field(default_factory=list)
    size: int = 0
    modified: float = 0.0
    generations: list[int] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.issues

    @property
    def file_count(self) -> int:
        total = 0
        for path in self.paths:
            total += sum(1 for p in path.rglob("*") if p.is_file()) if path.is_dir() else 1
        return total

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "format": self.format,
            "size": self.size,
            "modified": self.modified,
            "generations": self.generations,
            "file_count": self.file_count,
            "issues": self.issues,
            "complete": self.complete,
        }


def _tree_size(path: Path) -> tuple[int, float]:
    if path.is_file():
        stat = path.stat()
        return stat.st_size, stat.st_mtime
    size, newest = 0, 0.0
    for child in path.rglob("*"):
        if child.is_file():
            stat = child.stat()
            size += stat.st_size
            newest = max(newest, stat.st_mtime)
    return size, newest


def looks_like_world_folder(path: Path) -> bool:
    """True when a directory holds 1.0 world data."""
    if not path.is_dir():
        return False
    for child in path.iterdir():
        if child.is_file() and child.suffix in FOLDER_MARKERS:
            return True
    return False


def _inspect_folder(path: Path) -> World:
    generations: dict[int, set[str]] = {}
    for child in path.iterdir():
        if not child.is_file():
            continue
        match = RE_GENERATION.match(child.name)
        if match:
            generations.setdefault(int(match.group("n")), set()).add(match.group("ext"))

    size, modified = _tree_size(path)
    issues: list[str] = []
    # A usable generation needs its data, its metadata and its completion
    # marker; Valheim writes the marker last, so one without it was interrupted.
    usable = sorted(n for n, parts in generations.items() if {"db2", "fwl2"} <= parts)
    if not generations:
        issues.append("no _main.<N>.db2 / .fwl2 files - this folder is not a Valheim 1.0 world")
    elif not usable:
        issues.append("no generation has both its .db2 and .fwl2")
    elif not any("ok" in generations[n] for n in usable):
        issues.append("no generation carries its .ok completion marker")

    return World(
        name=path.name,
        format=FORMAT_FOLDER,
        root=path,
        paths=[path],
        size=size,
        modified=modified,
        generations=usable,
        issues=issues,
    )


def _inspect_pair(db: Path) -> World:
    fwl = db.with_suffix(".fwl")
    paths = [db] + ([fwl] if fwl.is_file() else [])
    size = sum(p.stat().st_size for p in paths)
    modified = max(p.stat().st_mtime for p in paths)
    issues = [] if fwl.is_file() else ["the .fwl is missing, so the world cannot be loaded"]
    return World(
        name=db.stem,
        format=FORMAT_PAIR,
        root=db.parent,
        paths=paths,
        size=size,
        modified=modified,
        issues=issues,
    )


def world_dirs(savedir: Path) -> list[Path]:
    return [savedir / name for name in WORLD_DIRS]


def default_world_dir(savedir: Path) -> Path:
    """Where a new world should be written."""
    for candidate in world_dirs(savedir):
        if candidate.is_dir():
            return candidate
    return savedir / WORLD_DIRS[0]


def discover(savedir: Path) -> list[World]:
    """Every world under *savedir*, in either format, excluding backups."""
    found: dict[str, World] = {}
    for directory in world_dirs(savedir):
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            if RE_BACKUP_NAME.match(entry.stem if entry.is_file() else entry.name):
                continue
            if entry.is_dir() and looks_like_world_folder(entry):
                found.setdefault(entry.name, _inspect_folder(entry))
            elif entry.is_file() and entry.suffix == ".db":
                found.setdefault(entry.stem, _inspect_pair(entry))
    return sorted(found.values(), key=lambda w: w.name.lower())


def find(savedir: Path, name: str) -> World | None:
    """The world the server would load for ``-world <name>``."""
    return next((w for w in discover(savedir) if w.name == name), None)


def copy_world(world: World, destination: Path) -> None:
    """Copy a world whole into *destination*, preserving its shape."""
    destination.mkdir(parents=True, exist_ok=True)
    for path in world.paths:
        target = destination / path.name
        if path.is_dir():
            shutil.copytree(path, target, dirs_exist_ok=True)
        else:
            shutil.copy2(path, target)


def export_world(world: World, destination: Path) -> Path:
    """Zip *world* whole into *destination* and return the archive path.

    The archive holds the world folder itself, not its contents, which is
    exactly the shape :func:`identify` reads back -- so a world exported from
    one instance imports into another with nothing to unwrap by hand. A legacy
    pair is written as the two files it is.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in world.paths:
            if path.is_dir():
                for child in sorted(path.rglob("*")):
                    if child.is_file() and not child.is_symlink():
                        relative = child.relative_to(path).as_posix()
                        archive.write(child, f"{world.name}/{relative}")
            elif path.is_file():
                archive.write(path, path.name)
    return destination


def remove_world(world: World) -> None:
    for path in world.paths:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)


def identify(staging: Path) -> tuple[str, str, Path]:
    """Work out what world an unpacked upload contains.

    Returns ``(name, format, source)`` where *source* is the directory or file
    to install. Handles a zipped world folder, a zip of that folder's contents,
    and the legacy pair, with or without a wrapping directory.
    """
    entries = [p for p in staging.iterdir() if not p.name.startswith("__MACOSX")]
    # A single wrapper directory (what zipping a world folder produces).
    while len(entries) == 1 and entries[0].is_dir() and not looks_like_world_folder(entries[0]):
        staging = entries[0]
        entries = [p for p in staging.iterdir() if not p.name.startswith("__MACOSX")]

    for entry in entries:
        if entry.is_dir() and looks_like_world_folder(entry):
            return entry.name, FORMAT_FOLDER, entry
    if looks_like_world_folder(staging):
        # The archive held the world's *contents*, so it carries no name.
        return "", FORMAT_FOLDER, staging

    for entry in entries:
        if entry.is_file() and entry.suffix == ".db":
            return entry.stem, FORMAT_PAIR, entry

    raise WorldError(
        "No Valheim world found in that upload. Expected a world folder with "
        "_main.<N>.db2 / .fwl2 and .chunk files, or a legacy .db + .fwl pair."
    )


def install(savedir: Path, staging: Path, *, name: str = "", overwrite: bool = False) -> World:
    """Install an unpacked upload into *savedir* as a world."""
    detected, world_format, source = identify(staging)
    final_name = (name or detected).strip()
    if not final_name:
        raise WorldError(
            "That archive holds a world's contents but not its folder, so the "
            "world name is unknown. Give a name, or zip the world folder itself."
        )
    if "/" in final_name or "\\" in final_name or final_name in (".", ".."):
        raise WorldError(f"{final_name!r} is not a usable world name.")

    target_dir = default_world_dir(savedir)
    target_dir.mkdir(parents=True, exist_ok=True)

    existing = find(savedir, final_name)
    if existing is not None and not overwrite:
        raise WorldError(
            f"A world named {final_name!r} is already here. Confirm the "
            "replacement to overwrite it."
        )
    if existing is not None:
        remove_world(existing)

    if world_format == FORMAT_FOLDER:
        destination = target_dir / final_name
        shutil.rmtree(destination, ignore_errors=True)
        shutil.copytree(source, destination)
    else:
        shutil.copy2(source, target_dir / f"{final_name}.db")
        fwl = source.with_suffix(".fwl")
        if fwl.is_file():
            shutil.copy2(fwl, target_dir / f"{final_name}.fwl")

    installed = find(savedir, final_name)
    if installed is None:
        raise WorldError("The world was copied but cannot be read back; upload looks incomplete.")
    return installed
