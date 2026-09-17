"""Checking that the server's shared libraries actually resolve.

Valheim loads Steam's ``steamclient.so`` at runtime. If one of *its*
dependencies is missing, the load fails quietly: the game starts, accepts
direct connections and plays fine, while Steam never initialises -- so the
query port stays silent and the server never appears in the browser. Nothing
in the console says why.

``ldd`` answers it directly, as long as it is run with the same library path
the server gets. Without that, libraries that live inside the install
directory look missing when they are not.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

from .config import Settings

#: ``libfoo.so.1 => not found``
RE_MISSING = re.compile(r"^\s*(\S+)\s*=>\s*not found", re.MULTILINE)

#: Files worth checking, relative to the game directory. The Steam libraries
#: matter most: they are the ones that fail without saying so.
CHECKED = (
    "valheim_server.x86_64",
    "linux64/steamclient.so",
    "linux64/libsteam_api.so",
)


@dataclass(slots=True)
class LibraryReport:
    checked: list[str]
    missing: dict[str, list[str]]
    available: bool = True
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.available and not self.missing

    @property
    def all_missing(self) -> list[str]:
        seen: list[str] = []
        for libs in self.missing.values():
            for lib in libs:
                if lib not in seen:
                    seen.append(lib)
        return seen

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "available": self.available,
            "reason": self.reason,
            "checked": self.checked,
            "missing": self.missing,
            "all_missing": self.all_missing,
        }


def check_libraries(settings: Settings) -> LibraryReport:
    """Report shared libraries the server or Steam cannot resolve."""
    game_dir = settings.game_dir
    if not game_dir.is_dir():
        return LibraryReport([], {}, available=False, reason="the server is not installed yet")
    if shutil.which("ldd") is None:
        return LibraryReport([], {}, available=False, reason="ldd is not available here")

    # Match the environment the server is launched with, or libraries that ship
    # inside the install directory are reported missing when they are present.
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = ":".join(
        [str(game_dir / "linux64"), str(game_dir), env.get("LD_LIBRARY_PATH", "")]
    ).strip(":")

    checked: list[str] = []
    missing: dict[str, list[str]] = {}
    for relative in CHECKED:
        target = game_dir / relative
        if not target.is_file():
            continue
        checked.append(relative)
        try:
            result = subprocess.run(
                ["ldd", str(target)],
                capture_output=True, text=True, timeout=20, env=env, cwd=str(game_dir),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            missing[relative] = [f"could not check: {exc}"]
            continue
        found = RE_MISSING.findall(result.stdout + result.stderr)
        if found:
            missing[relative] = sorted(set(found))

    if not checked:
        return LibraryReport([], {}, available=False, reason="no server binaries found to check")
    return LibraryReport(checked, missing)


#: Debian package names for the libraries that usually turn up missing, so the
#: report can say what to install rather than only what is absent.
PACKAGE_HINTS = {
    "libsdl2": "libsdl2-2.0-0",
    "libcurl": "libcurl4",
    "libpulse": "libpulse0",
    "libatomic": "libatomic1",
    "libstdc++": "libstdc++6",
    "libgcc_s": "libgcc-s1",
    "libz": "zlib1g",
    "libssl": "libssl3",
    "libcrypto": "libssl3",
}


def package_for(library: str) -> str:
    """Debian package providing *library*, or "" if we have no hint.

    Matched case-insensitively: the real file is ``libSDL2-2.0.so.0``, so a
    lowercase comparison would silently never match the one library most
    likely to be missing.
    """
    name = library.lower()
    for prefix, package in PACKAGE_HINTS.items():
        if name.startswith(prefix):
            return package
    return ""
