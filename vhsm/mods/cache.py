"""Shared package cache.

r2modman downloads a package once and then installs it into any number of
profiles from a local cache. We do the same: ``cache/<namespace-name>/<version>/``
holds the extracted package, so installing the same mod into a second instance
is a file copy, works offline, and never re-downloads.
"""

from __future__ import annotations

import asyncio
import shutil
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable

import httpx

from .thunderstore import PackageVersion

#: Written once extraction finished, so a partially extracted directory left
#: behind by a crash is never mistaken for a usable cache entry. It holds
#: :data:`EXTRACT_VERSION`, so an entry unpacked by an older and
#: differently-behaving unpacker is re-extracted rather than trusted.
COMPLETE_MARKER = ".vhsm-complete"
#: Bumped whenever :func:`_safe_extract` would lay a package out differently.
EXTRACT_VERSION = 2
MAX_PACKAGE_BYTES = 512 * 1024 * 1024

ProgressHook = Callable[[str], None]


class CacheError(RuntimeError):
    pass


def entry_path(cache_dir: Path, version: PackageVersion) -> Path:
    return cache_dir / version.package_full_name / version.version_number


def is_cached(cache_dir: Path, version: PackageVersion) -> bool:
    try:
        marker = (entry_path(cache_dir, version) / COMPLETE_MARKER).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return False
    return marker.strip() == str(EXTRACT_VERSION)


def mark_complete(entry: Path) -> None:
    """Record that *entry* holds a package unpacked by this version of the rules."""
    (entry / COMPLETE_MARKER).write_text(f"{EXTRACT_VERSION}\n", encoding="utf-8")


def member_path(name: str) -> str:
    """An archive member's name as a POSIX path.

    A zip is specified to separate directories with ``/`` and nothing else
    (APPNOTE 4.4.17.1), but PowerShell's ``Compress-Archive`` writes Windows
    separators instead, and plenty of Valheim mods are packaged with it --
    ValheimRAFT among them. Windows unpackers split on either separator, so
    those packages look well-formed there; taking the names literally on Linux
    instead gives one flat file *called* ``plugins\\ValheimRAFT.dll``, with no
    ``plugins`` directory at all. BepInEx still finds such a file by its
    ``*.dll`` glob and loads the plugin, but it can no longer resolve a
    *library* beside it -- ``Zolantris.Shared.dll`` becomes
    ``plugins\\Zolantris.Shared.dll``, which is not the file name any
    assembly reference asks for -- so the mod loads and then dies on a
    missing assembly. Normalising here, where member names first become
    paths, keeps both separators meaning the same thing they mean on Windows.
    """
    return name.replace("\\", "/")


def _safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
    """Extract, refusing any member that would escape *destination* (zip slip)."""
    destination = destination.resolve()
    total = 0
    for info in archive.infolist():
        name = member_path(info.filename)
        if not name or name.endswith("/"):
            continue
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise CacheError(f"unsafe archive member: {info.filename}")
        total += info.file_size
        if total > MAX_PACKAGE_BYTES:
            raise CacheError("package is unreasonably large")

        target = (destination / relative).resolve()
        if destination not in target.parents:
            raise CacheError(f"archive member escapes destination: {name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info) as source, target.open("wb") as sink:
            shutil.copyfileobj(source, sink)


def _extract_blocking(payload_path: Path, destination: Path) -> None:
    staging = destination.with_name(destination.name + ".partial")
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(payload_path) as archive:
            _safe_extract(archive, staging)
    except zipfile.BadZipFile as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise CacheError(f"downloaded file is not a valid zip: {exc}") from exc

    mark_complete(staging)
    if destination.exists():
        shutil.rmtree(destination, ignore_errors=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging.rename(destination)


async def ensure_cached(
    cache_dir: Path,
    version: PackageVersion,
    progress: ProgressHook | None = None,
) -> Path:
    """Return the cached package directory, downloading it if necessary."""
    destination = entry_path(cache_dir, version)
    if is_cached(cache_dir, version):
        return destination

    def report(message: str) -> None:
        if progress:
            progress(message)

    if not version.download_url:
        raise CacheError(f"no download url for {version.full_name}")

    report(f"downloading {version.full_name}")
    payload_path = destination.parent / f"{version.version_number}.zip.part"
    payload_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        async with httpx.AsyncClient(timeout=180, follow_redirects=True) as client:
            async with client.stream("GET", version.download_url) as response:
                response.raise_for_status()
                written = 0
                with payload_path.open("wb") as sink:
                    async for chunk in response.aiter_bytes(64 * 1024):
                        written += len(chunk)
                        if written > MAX_PACKAGE_BYTES:
                            raise CacheError("package exceeded the size limit")
                        sink.write(chunk)
        report(f"extracting {version.full_name} ({written} bytes)")
        await asyncio.to_thread(_extract_blocking, payload_path, destination)
    except httpx.HTTPError as exc:
        raise CacheError(f"download failed for {version.full_name}: {exc}") from exc
    finally:
        payload_path.unlink(missing_ok=True)

    return destination


def store_upload(cache_dir: Path, version: PackageVersion, payload_path: Path) -> Path:
    """Add a manually uploaded zip to the cache under a synthetic version."""
    destination = entry_path(cache_dir, version)
    _extract_blocking(payload_path, destination)
    return destination


def cache_size(cache_dir: Path) -> int:
    if not cache_dir.is_dir():
        return 0
    return sum(f.stat().st_size for f in cache_dir.rglob("*") if f.is_file())


def clear_cache(cache_dir: Path) -> None:
    for child in cache_dir.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
