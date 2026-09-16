"""Where files from a Thunderstore package belong inside a profile.

Mirrors r2modman's *install rules*: a package is an opaque zip, and the rules
map its top-level folders onto the BepInEx tree. Anything unrecognised falls
through to ``BepInEx/plugins/<namespace>-<name>/``, which is what the vast
majority of Valheim mods expect.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

#: Package metadata that must never be copied into the profile.
#: (r2modman calls these ``relativeFileExclusions``.)
EXCLUDED_FILES = {
    "manifest.json",
    "icon.png",
    "readme.md",
    "changelog.md",
    "license",
    "license.txt",
    "license.md",
}


@dataclass(frozen=True, slots=True)
class Route:
    """A destination inside the profile.

    ``per_mod`` mirrors r2modman's SUBDIR tracking: files get their own
    ``<namespace>-<name>`` folder, so uninstalling is a single directory
    removal and two mods can never fight over a filename. Routes with
    ``per_mod=False`` (core, config) are flat and shared.
    """

    destination: str
    per_mod: bool = True
    #: Never overwrite existing files -- used for config, so a user's tuned
    #: settings survive a mod update.
    preserve_existing: bool = False


#: Top-level folder inside the package -> route.
FOLDER_ROUTES: dict[str, Route] = {
    "plugins": Route("BepInEx/plugins"),
    "patchers": Route("BepInEx/patchers"),
    "monomod": Route("BepInEx/monomod"),
    "core": Route("BepInEx/core", per_mod=False),
    "config": Route("BepInEx/config", per_mod=False, preserve_existing=True),
}

#: Anything not matched above.
DEFAULT_ROUTE = Route("BepInEx/plugins")


def is_excluded(relative: PurePosixPath) -> bool:
    return relative.name.lower() in EXCLUDED_FILES and len(relative.parts) == 1


def resolve(relative: PurePosixPath, mod_folder: str) -> PurePosixPath | None:
    """Map a path inside the package to a path inside the profile.

    Returns ``None`` for files that should not be installed.
    """
    if is_excluded(relative):
        return None

    parts = relative.parts
    if not parts:
        return None

    head = parts[0].lower()
    route = FOLDER_ROUTES.get(head)
    if route is not None:
        remainder = PurePosixPath(*parts[1:]) if len(parts) > 1 else None
        if remainder is None:
            return None
        base = PurePosixPath(route.destination)
        return base / mod_folder / remainder if route.per_mod else base / remainder

    # Unrecognised: everything keeps its shape under the mod's own folder.
    return PurePosixPath(DEFAULT_ROUTE.destination) / mod_folder / relative


#: Destinations whose existing files must never be overwritten, and whose
#: contents belong to the operator rather than to any one mod.
PRESERVED_DESTINATIONS = tuple(
    PurePosixPath(route.destination)
    for route in FOLDER_ROUTES.values()
    if route.preserve_existing
)


def is_preserved(target_relative: PurePosixPath) -> bool:
    """True for a *destination* path that holds operator-owned files.

    Applied to the resolved destination rather than the source path, so a
    config file lands in the same bucket whether it arrived through the
    ``config/`` route or from a package that installs to the profile root.
    """
    return any(target_relative.is_relative_to(base) for base in PRESERVED_DESTINATIONS)
