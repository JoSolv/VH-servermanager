"""Checking that the server's shared libraries actually resolve.

Valheim loads Steam's ``steamclient.so`` at runtime. If one of *its*
dependencies is missing, the load fails quietly: the game starts, accepts
direct connections and plays fine, while Steam never initialises -- so the
query port stays silent and the server never appears in the browser. Nothing
in the console says why.

The crossplay backend fails in the same shape and is worth checking for the
same reason. Crossplay runs on Microsoft's PlayFab Party, whose native half is
``libparty.so`` under ``valheim_server_Data/Plugins``; it is loaded by name at
run time, not by the linker, and it is documented as failing on Linux for
missing symbols such as ``__atomic_load`` -- i.e. for want of ``libatomic1``.
When it does, the server still boots, still logs into PlayFab over HTTPS from
C#, and still registers its address, because those are not this library; only
the Party network never comes up, so no join code is ever issued.

``ldd`` answers it directly, as long as it is run with the same library path
the server gets. Without that, libraries that live inside the install
directory look missing when they are not.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
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

#: Unity loads the files under ``Plugins`` by name at run time rather than
#: through the linker, so a broken one of them is invisible to everything
#: except the feature that needs it. ``libparty.so``, which crossplay runs on,
#: lives here. Globbed rather than named: the layout has moved between builds,
#: and a path that stops matching would silently check nothing.
PLUGIN_GLOBS = (
    "valheim_server_Data/Plugins/*.so",
    "valheim_server_Data/Plugins/**/*.so",
)

#: Upper bound on plugins checked, so an unexpected install layout cannot turn
#: one page load into hundreds of subprocess calls.
PLUGIN_LIMIT = 40


def plugin_libraries(game_dir: Path) -> list[str]:
    """Shared objects shipped under ``Plugins``, relative to *game_dir*."""
    found: list[str] = []
    for pattern in PLUGIN_GLOBS:
        for path in sorted(game_dir.glob(pattern)):
            if not path.is_file():
                continue
            relative = str(path.relative_to(game_dir))
            if relative not in found:
                found.append(relative)
    return found[:PLUGIN_LIMIT]


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
    for relative in (*CHECKED, *plugin_libraries(game_dir)):
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


# --------------------------------------------------------------------------- #
# reaching PlayFab at all
# --------------------------------------------------------------------------- #
#: Microsoft serves the PlayFab API from a wildcard record: every title gets
#: its own subdomain and the bare domain has no address of its own, so a check
#: has to ask for *some* subdomain or it fails for the wrong reason. Which one
#: is arbitrary -- they all resolve to the same front door.
PLAYFAB_HOST = "title.playfabapi.com"
PLAYFAB_PORT = 443

#: What a DNS filter answers with when it is blocking a name rather than
#: failing to find one. Pi-hole's default is 0.0.0.0; others use loopback.
SINKHOLES = {"0.0.0.0", "127.0.0.1", "::", "::1"}


@dataclass(slots=True)
class EgressReport:
    """Whether PlayFab is reachable from wherever the server runs."""

    host: str = PLAYFAB_HOST
    port: int = PLAYFAB_PORT
    addresses: list[str] = field(default_factory=list)
    ok: bool = False
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "addresses": self.addresses,
            "ok": self.ok,
            "detail": self.detail,
        }


def check_playfab_egress(timeout: float = 3.0) -> EgressReport:
    """Can this host reach PlayFab's API at all?

    Crossplay's HTTPS half -- logging in, registering the server's address --
    talks to ``*.playfabapi.com``. A DNS filter or ad blocker on the path
    answers for that name with nothing, or with an address that refuses the
    connection, and an ad blocker is what broke the public-IP lookup on this
    project once already. It is cheap to rule out rather than argue about.

    Reaching it does not prove crossplay will work: the part that issues a
    join code is a PlayFab Party network built over UDP to Azure relays, and
    nothing here can test that from outside the game. A *failure* is
    conclusive, though, which is what makes the check worth running.
    """
    report = EgressReport()
    try:
        infos = socket.getaddrinfo(PLAYFAB_HOST, PLAYFAB_PORT, type=socket.SOCK_STREAM)
    except OSError as exc:
        report.detail = (
            f"{PLAYFAB_HOST} does not resolve here ({exc}). A DNS filter, an ad "
            "blocker or a broken resolver on this host would do that."
        )
        return report

    report.addresses = sorted({info[4][0] for info in infos})
    blocked = [a for a in report.addresses if a in SINKHOLES]
    if blocked and len(blocked) == len(report.addresses):
        report.detail = (
            f"{PLAYFAB_HOST} resolves to {', '.join(blocked)}, which is not an "
            "address that goes anywhere -- something on this host's DNS path is "
            "blocking the name rather than failing to find it."
        )
        return report

    try:
        with socket.create_connection((PLAYFAB_HOST, PLAYFAB_PORT), timeout):
            pass
    except OSError as exc:
        report.detail = (
            f"{PLAYFAB_HOST} resolves to {', '.join(report.addresses)} but the "
            f"connection to port {PLAYFAB_PORT} failed ({exc})."
        )
        return report

    report.ok = True
    report.detail = (
        f"{PLAYFAB_HOST} resolves to {', '.join(report.addresses)} and answers "
        f"on {PLAYFAB_PORT}."
    )
    return report
