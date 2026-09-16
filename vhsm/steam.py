"""Bootstrapping steamcmd and installing/updating the dedicated server.

One shared game install is kept for every instance. Instances differ only by
their own directory (saves, config and BepInEx profile), which keeps updates
to a single ~2 GB download no matter how many servers are configured.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import AsyncIterator, Callable

import httpx

from .config import Settings, VALHEIM_SERVER_APPID

STEAMCMD_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz"
ProgressHook = Callable[[str], None]

RE_BUILDID = re.compile(r'"buildid"\s*"(\d+)"')


class SteamError(RuntimeError):
    pass


async def install_steamcmd(settings: Settings, progress: ProgressHook) -> None:
    """Download and unpack steamcmd if it is not already present."""
    if settings.steamcmd_bin.is_file():
        progress("steamcmd already installed")
        return

    settings.steamcmd_dir.mkdir(parents=True, exist_ok=True)
    progress(f"downloading steamcmd from {STEAMCMD_URL}")
    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            response = await client.get(STEAMCMD_URL)
            response.raise_for_status()
            payload = response.content
    except httpx.HTTPError as exc:
        raise SteamError(f"could not download steamcmd: {exc}") from exc

    progress(f"unpacking {len(payload)} bytes")
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as handle:
        handle.write(payload)
        archive_path = Path(handle.name)
    try:
        with tarfile.open(archive_path) as archive:
            # Refuse absolute or traversing members before extracting.
            for member in archive.getmembers():
                if member.name.startswith("/") or ".." in Path(member.name).parts:
                    raise SteamError(f"unsafe archive member: {member.name}")
            archive.extractall(settings.steamcmd_dir)
    finally:
        archive_path.unlink(missing_ok=True)

    if not settings.steamcmd_bin.is_file():
        raise SteamError("steamcmd.sh missing after extraction")
    settings.steamcmd_bin.chmod(0o755)
    progress("steamcmd installed")


async def update_server(settings: Settings, validate: bool = False) -> AsyncIterator[str]:
    """Install or update the Valheim dedicated server, yielding output lines."""
    if not settings.steamcmd_bin.is_file():
        raise SteamError("steamcmd is not installed yet")

    settings.game_dir.mkdir(parents=True, exist_ok=True)
    command = [
        str(settings.steamcmd_bin),
        "+force_install_dir", str(settings.game_dir),
        "+login", "anonymous",
        "+app_update", VALHEIM_SERVER_APPID,
    ]
    if validate:
        command.append("validate")
    command.append("+quit")

    env = dict(os.environ)
    env.setdefault("HOME", str(settings.steamcmd_dir))

    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(settings.steamcmd_dir),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL,
    )
    assert process.stdout is not None
    async for raw in process.stdout:
        yield raw.decode("utf-8", errors="replace").rstrip("\r\n")

    code = await process.wait()
    if code != 0:
        yield f"[manager] steamcmd exited with code {code}"
        raise SteamError(f"steamcmd failed with exit code {code}")

    binary = settings.game_dir / "valheim_server.x86_64"
    if binary.is_file():
        binary.chmod(0o755)
    yield "[manager] server files up to date"


def server_status(settings: Settings) -> dict[str, object]:
    """Summary shown on the Settings page."""
    binary = settings.game_dir / "valheim_server.x86_64"
    installed = binary.is_file()
    size = 0
    if settings.game_dir.is_dir():
        size = sum(
            f.stat().st_size
            for f in settings.game_dir.rglob("*")
            if f.is_file() and not f.is_symlink()
        )
    return {
        "steamcmd_installed": settings.steamcmd_bin.is_file(),
        "steamcmd_path": str(settings.steamcmd_bin),
        "server_installed": installed,
        "server_path": str(binary),
        "install_size": size,
        "fake_server": settings.fake_server,
        "disk_free": shutil.disk_usage(settings.data_root).free
        if settings.data_root.exists()
        else 0,
    }


def write_steam_appid(settings: Settings) -> None:
    """Drop ``steam_appid.txt`` next to the binary.

    The Steam game-server API reads it from the working directory. Without it
    (or the matching env var) initialisation can half-fail: the game still
    accepts direct connections while the query port stays silent.
    """
    if not settings.game_dir.is_dir():
        return
    target = settings.game_dir / "steam_appid.txt"
    try:
        if target.read_text(encoding="utf-8").strip() == VALHEIM_CLIENT_APPID:
            return
    except (OSError, ValueError):
        pass
    try:
        target.write_text(f"{VALHEIM_CLIENT_APPID}\n", encoding="utf-8")
    except OSError:
        pass


def installed_build_id(settings: Settings) -> str:
    """Build id of the install on disk, from steamcmd's app manifest."""
    manifest = (
        settings.game_dir / "steamapps" / f"appmanifest_{VALHEIM_SERVER_APPID}.acf"
    )
    try:
        match = RE_BUILDID.search(manifest.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""
    return match.group(1) if match else ""


def _parse_public_build_id(text: str) -> str:
    """Pull the public branch's build id out of ``app_info_print`` output."""
    branches = text.find('"branches"')
    if branches < 0:
        return ""
    # The public block holds only scalars, so stopping at the first brace is safe.
    block = re.search(r'"public"\s*\{(.*?)\}', text[branches:], re.S)
    if not block:
        return ""
    match = RE_BUILDID.search(block.group(1))
    return match.group(1) if match else ""


async def latest_build_id(settings: Settings) -> str:
    """Ask Steam for the newest published build id of the dedicated server."""
    if not settings.steamcmd_bin.is_file():
        raise SteamError("steamcmd is not installed yet")

    env = dict(os.environ)
    env.setdefault("HOME", str(settings.steamcmd_dir))
    process = await asyncio.create_subprocess_exec(
        str(settings.steamcmd_bin),
        "+login", "anonymous",
        # Without a forced refresh steamcmd happily reports its cached copy.
        "+app_info_update", "1",
        "+app_info_print", VALHEIM_SERVER_APPID,
        "+quit",
        cwd=str(settings.steamcmd_dir),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=180)
    except asyncio.TimeoutError as exc:
        process.kill()
        raise SteamError("timed out asking Steam for the latest build") from exc

    build = _parse_public_build_id(stdout.decode("utf-8", errors="replace"))
    if not build:
        raise SteamError("could not read the latest build id from steamcmd output")
    return build
