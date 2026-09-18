"""Exporting and importing a whole server **instance**.

An instance is a world *plus* everything wrapped around it: its configuration,
its access lists, the roster of everyone who has played on it, its mods and
their tuning, and its snapshots. A world on its own is handled by
:mod:`vhsm.worlds`; this module moves the server that owns one.

One instance is already a self-contained directory, so an export is that
directory in a zip with a manifest describing what is inside. Importing one
therefore yields a *clone* of the original rather than a reconstruction of it,
which is the whole point: a restored server has the same players, the same
mods and the same rollback history as the one it came from.

The only thing left out by default is ``logs/`` -- the console transcript of
the original server's past runs, which is a record of that machine rather than
state the clone uses, and which can dwarf the world it sits beside. It can be
asked for when the archive is meant as a forensic copy.
"""

from __future__ import annotations

import json
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator

from . import __version__
from .instance import InstanceConfig, InstanceLayout
from .util import read_json

#: Bumped when the layout inside the archive changes incompatibly.
ARCHIVE_VERSION = 1
MANIFEST_NAME = "vhsm-manifest.json"
SUFFIX = ".vhsm.zip"
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024 * 1024
ProgressHook = Callable[[str], None]


class ArchiveError(RuntimeError):
    pass


@dataclass(slots=True)
class ArchiveInfo:
    """Summary of an archive, read without extracting it."""

    version: int
    name: str
    world: str
    exported_at: float
    exported_by: str
    mods: list[dict[str, Any]]
    includes: dict[str, bool]
    config: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "name": self.name,
            "world": self.world,
            "exported_at": self.exported_at,
            "exported_by": self.exported_by,
            "mods": self.mods,
            "includes": self.includes,
        }


def _walk(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            yield path


def _should_include(relative: PurePosixPath, include_logs: bool) -> bool:
    """Whether one instance-relative path belongs in the archive.

    An export is a clone, so the default is to take everything. Only the
    console transcript is held back, and only because it describes the runs of
    the server being copied rather than anything the copy will use.
    """
    parts = relative.parts
    if not parts:
        return False
    if parts[0] == "logs":
        return include_logs
    return True


def export_instance(
    layout: InstanceLayout,
    config: InstanceConfig,
    destination: Path,
    *,
    include_logs: bool = False,
    progress: ProgressHook | None = None,
) -> Path:
    """Write an archive of *layout* to *destination*."""
    mods = read_json(layout.mods_manifest, {}) or {}
    manifest = {
        "version": ARCHIVE_VERSION,
        "kind": "vhsm-instance",
        "exported_at": time.time(),
        "exported_by": f"vhsm {__version__}",
        "name": config.name,
        "world": config.world,
        "config": config.to_dict(),
        "mods": mods.get("mods", []),
        "includes": {"mods": True, "backups": True, "players": True, "logs": include_logs},
    }

    destination.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2, sort_keys=True))
        for path in _walk(layout.root):
            relative = PurePosixPath(path.relative_to(layout.root).as_posix())
            if not _should_include(relative, include_logs):
                continue
            archive.write(path, str(relative))
            written += 1
    if progress:
        progress(f"exported {written} file(s) to {destination.name}")
    return destination


def read_info(archive_path: Path) -> ArchiveInfo:
    """Read an archive's manifest without extracting anything."""
    try:
        with zipfile.ZipFile(archive_path) as archive:
            try:
                raw = archive.read(MANIFEST_NAME)
            except KeyError as exc:
                raise ArchiveError(
                    "not a vhsm instance archive (no manifest inside)"
                ) from exc
            payload = json.loads(raw.decode("utf-8"))
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f"not a readable zip file: {exc}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArchiveError(f"manifest is corrupt: {exc}") from exc

    if not isinstance(payload, dict) or payload.get("kind") != "vhsm-instance":
        raise ArchiveError("this zip is not a vhsm instance archive")
    version = int(payload.get("version") or 0)
    if version > ARCHIVE_VERSION:
        raise ArchiveError(
            f"archive format v{version} is newer than this manager understands "
            f"(v{ARCHIVE_VERSION}); upgrade vhsm first"
        )

    config = payload.get("config")
    if not isinstance(config, dict):
        raise ArchiveError("archive manifest has no instance configuration")
    return ArchiveInfo(
        version=version,
        name=str(payload.get("name") or config.get("name") or "Imported server"),
        world=str(payload.get("world") or config.get("world") or "Dedicated"),
        exported_at=float(payload.get("exported_at") or 0),
        exported_by=str(payload.get("exported_by") or "unknown"),
        mods=list(payload.get("mods") or []),
        includes=dict(payload.get("includes") or {}),
        config=config,
    )


def extract_zip(archive_path: Path, target: Path, skip: frozenset[str] = frozenset()) -> int:
    """Extract a zip into *target*, refusing any member that escapes it.

    Paths inside an archive are attacker-controlled in the same way a
    downloaded mod zip is, so every member is checked before anything is
    written. Shared by instance import and world upload.
    """
    target = target.resolve()
    target.mkdir(parents=True, exist_ok=True)
    extracted = 0
    total = 0

    try:
        archive_file = zipfile.ZipFile(archive_path)
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f"not a readable zip file: {exc}") from exc

    with archive_file as archive:
        for info in archive.infolist():
            if info.is_dir() or info.filename in skip:
                continue
            relative = PurePosixPath(info.filename)
            if relative.is_absolute() or ".." in relative.parts or info.filename.startswith("\\"):
                raise ArchiveError(f"unsafe archive member: {info.filename}")
            total += info.file_size
            if total > MAX_ARCHIVE_BYTES:
                raise ArchiveError("archive contents exceed the size limit")

            destination = (target / relative).resolve()
            if target not in destination.parents:
                raise ArchiveError(f"archive member escapes the target: {info.filename}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, destination.open("wb") as sink:
                while chunk := source.read(1 << 20):
                    sink.write(chunk)
            extracted += 1
    return extracted


def extract_into(archive_path: Path, target: Path) -> int:
    """Extract an instance archive's payload, leaving the manifest behind."""
    return extract_zip(archive_path, target, skip=frozenset({MANIFEST_NAME}))


def suggested_filename(config: InstanceConfig) -> str:
    return f"{config.slug}-{time.strftime('%Y%m%d-%H%M%S')}{SUFFIX}"
