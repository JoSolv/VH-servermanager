"""Global settings and on-disk layout.

Everything the manager owns lives under a single data root so the whole
deployment can be backed up or moved by copying one directory.

    <data_root>/
        steam/            steamcmd itself
        valheim/          the shared dedicated-server install (app 896660)
        cache/            Thunderstore package cache, r2modman style
        instances/<id>/   one directory per server instance
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__

#: Steam app id of the Valheim *dedicated server*.
VALHEIM_SERVER_APPID = "896660"
#: Steam app id of the Valheim *client*. The server binary refuses to boot
#: unless SteamAppId points at the client id.
VALHEIM_CLIENT_APPID = "892970"

#: Thunderstore community slug used to build API urls.
THUNDERSTORE_COMMUNITY = "valheim"


def build_info() -> dict[str, str]:
    """Which build of the manager this actually is.

    A mutable tag like ``:latest`` makes "did my update land?" genuinely hard
    to answer from the outside, so the image stamps its commit and build time
    in and they are shown in the UI and logged at startup. Running from a
    source checkout leaves them blank, which is itself the answer.
    """
    commit = os.environ.get("VHSM_BUILD_SHA", "").strip()
    return {
        "version": __version__,
        "commit": commit,
        "short_commit": commit[:7],
        "built_at": os.environ.get("VHSM_BUILD_TIME", "").strip(),
        "source": "container image" if commit else "source checkout",
    }


class DataRootError(RuntimeError):
    """The data directory cannot be created or written."""


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser().resolve() if raw else default


@dataclass(slots=True)
class Settings:
    """Runtime configuration, overridable through environment variables."""

    data_root: Path = field(
        default_factory=lambda: _env_path(
            "VHSM_DATA_ROOT", Path.cwd() / "data"
        )
    )
    host: str = field(default_factory=lambda: os.environ.get("VHSM_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.environ.get("VHSM_PORT", "8080")))

    #: Seconds between metric samples pushed to connected browsers.
    sample_interval: float = field(
        default_factory=lambda: float(os.environ.get("VHSM_SAMPLE_INTERVAL", "2.0"))
    )
    #: How long a cached Thunderstore package index stays fresh, in seconds.
    index_ttl: float = field(
        default_factory=lambda: float(os.environ.get("VHSM_INDEX_TTL", "3600"))
    )
    #: Run instances against a stub binary instead of the real server. Lets the
    #: whole GUI be exercised without a ~2GB Steam download.
    fake_server: bool = field(
        default_factory=lambda: os.environ.get("VHSM_FAKE_SERVER", "") not in ("", "0")
    )

    @property
    def steamcmd_dir(self) -> Path:
        return self.data_root / "steam"

    @property
    def steamcmd_bin(self) -> Path:
        return self.steamcmd_dir / "steamcmd.sh"

    @property
    def game_dir(self) -> Path:
        """Shared dedicated-server install, reused by every instance."""
        return self.data_root / "valheim"

    @property
    def server_binary(self) -> Path:
        if self.fake_server:
            return Path(__file__).resolve().parent.parent / "tools" / "fake_valheim_server.py"
        return self.game_dir / "valheim_server.x86_64"

    @property
    def cache_dir(self) -> Path:
        """Extracted Thunderstore packages, shared by all instances."""
        return self.data_root / "cache"

    @property
    def home_dir(self) -> Path:
        """HOME for the processes we launch.

        steamcmd keeps its own state under ``$HOME/Steam`` and the server
        writes crash dumps relative to HOME, so both need somewhere writable.
        Inheriting the manager's HOME is not good enough: in a container it is
        typically ``/`` or ``/root``, which an unprivileged app user cannot
        write, and steamcmd then fails with "Missing file permissions" long
        after appearing to work.
        """
        return self.data_root / "home"

    @property
    def instances_dir(self) -> Path:
        return self.data_root / "instances"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_root,
            self.steamcmd_dir,
            self.cache_dir,
            self.instances_dir,
            self.home_dir,
        ):
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                # This is the first thing that happens on a misconfigured
                # container mount, so it is worth an answer rather than a
                # traceback: name the path, the user, and the usual cause.
                raise DataRootError(
                    f"Cannot create {path}: {exc.strerror or exc}.\n"
                    f"Running as uid {os.getuid()}, gid {os.getgid()}.\n"
                    "The data directory must be writable by that user. In a "
                    "container, check that the mounted dataset is owned by "
                    "your PUID/PGID."
                ) from exc


settings = Settings()
