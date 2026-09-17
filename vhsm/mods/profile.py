"""A mod profile: the set of mods installed into one instance.

Modelled on r2modman. The instance directory *is* the profile -- mods are
materialised into ``<instance>/BepInEx`` and recorded in ``mods.json`` with the
exact list of files each one owns, so uninstalling is precise rather than a
guess, and disabling is reversible.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from ..instance import InstanceLayout
from ..util import read_json, write_json
from . import rules
from .bepinex import BEPINEX_PACKAGE
from .cache import ensure_cached
from .thunderstore import (
    PackageVersion,
    ThunderstoreError,
    ThunderstoreIndex,
    parse_dependency,
)

MANIFEST_VERSION = 1
#: Suffix used to disable a file without deleting it, as r2modman does.
DISABLED_SUFFIX = ".old"
#: Guard against a malformed dependency graph.
MAX_DEPENDENCY_DEPTH = 24

ProgressHook = Callable[[str], None]


class ModError(RuntimeError):
    pass


@dataclass(slots=True)
class InstalledMod:
    namespace: str
    name: str
    version: str
    enabled: bool = True
    #: True when the mod was pulled in to satisfy another mod's dependency.
    dependency_only: bool = False
    description: str = ""
    icon: str = ""
    website_url: str = ""
    dependencies: list[str] = field(default_factory=list)
    #: Profile-relative paths this mod exclusively owns, in their *enabled*
    #: spelling. These are what enable/disable renames and uninstall deletes.
    files: list[str] = field(default_factory=list)
    #: Config files this mod shipped. Tracked but deliberately never renamed
    #: or deleted: they hold settings the operator has tuned, and BepInEx
    #: regenerates them anyway.
    config_files: list[str] = field(default_factory=list)
    installed_at: float = field(default_factory=time.time)

    @property
    def package_full_name(self) -> str:
        return f"{self.namespace}-{self.name}"

    @property
    def full_name(self) -> str:
        return f"{self.namespace}-{self.name}-{self.version}"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["package_full_name"] = self.package_full_name
        payload["full_name"] = self.full_name
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "InstalledMod":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in payload.items() if k in known})


class ModProfile:
    """Reads and mutates the mod set of a single instance."""

    def __init__(self, layout: InstanceLayout, cache_dir: Path, index: ThunderstoreIndex) -> None:
        self.layout = layout
        self.cache_dir = cache_dir
        self.index = index
        self._mods: list[InstalledMod] = []
        self.load()

    # ------------------------------------------------------------------ #
    # persistence
    # ------------------------------------------------------------------ #
    def load(self) -> None:
        payload = read_json(self.layout.mods_manifest, {}) or {}
        self._mods = [InstalledMod.from_dict(m) for m in payload.get("mods", [])]

    def save(self) -> None:
        write_json(
            self.layout.mods_manifest,
            {"version": MANIFEST_VERSION, "mods": [m.to_dict() for m in self._mods]},
        )

    # ------------------------------------------------------------------ #
    # queries
    # ------------------------------------------------------------------ #
    @property
    def mods(self) -> list[InstalledMod]:
        return list(self._mods)

    def get(self, package_full_name: str) -> InstalledMod | None:
        key = package_full_name.lower()
        for mod in self._mods:
            if mod.package_full_name.lower() == key:
                return mod
        return None

    def dependents_of(self, package_full_name: str) -> list[InstalledMod]:
        """Installed mods that declare a dependency on *package_full_name*."""
        key = package_full_name.lower()
        found = []
        for mod in self._mods:
            for dependency in mod.dependencies:
                try:
                    namespace, name, _ = parse_dependency(dependency)
                except ThunderstoreError:
                    continue
                if f"{namespace}-{name}".lower() == key:
                    found.append(mod)
                    break
        return found

    def updates_available(self) -> list[dict[str, str]]:
        """Installed mods whose Thunderstore latest differs from what is here."""
        updates = []
        for mod in self._mods:
            package = self.index.get(mod.package_full_name)
            latest = package.latest if package else None
            if latest and latest.version_number != mod.version:
                updates.append(
                    {
                        "package_full_name": mod.package_full_name,
                        "current": mod.version,
                        "latest": latest.version_number,
                    }
                )
        return updates

    # ------------------------------------------------------------------ #
    # dependency resolution
    # ------------------------------------------------------------------ #
    def _resolve_chain(self, version: PackageVersion) -> list[tuple[PackageVersion, bool]]:
        """Flatten a package and its dependencies, dependencies first.

        The boolean is ``dependency_only``. BepInEx is appended implicitly
        because a Valheim mod is unloadable without it and not every package
        bothers to declare it.
        """
        ordered: list[tuple[PackageVersion, bool]] = []
        seen: set[str] = set()

        def walk(candidate: PackageVersion, depth: int, is_dependency: bool) -> None:
            key = candidate.package_full_name.lower()
            if key in seen:
                return
            if depth > MAX_DEPENDENCY_DEPTH:
                raise ModError(f"dependency chain too deep at {candidate.full_name}")
            seen.add(key)
            for dependency in candidate.dependencies:
                resolved = self.index.resolve_dependency(dependency)
                if resolved is None:
                    raise ModError(
                        f"{candidate.full_name} needs {dependency}, which is not on Thunderstore"
                    )
                walk(resolved, depth + 1, True)
            ordered.append((candidate, is_dependency))

        walk(version, 0, False)

        if not any(v.package_full_name.lower() == BEPINEX_PACKAGE.lower() for v, _ in ordered):
            package = self.index.get(BEPINEX_PACKAGE)
            if package and package.latest and not self.get(BEPINEX_PACKAGE):
                ordered.insert(0, (package.latest, True))
        return ordered

    # ------------------------------------------------------------------ #
    # installing
    # ------------------------------------------------------------------ #
    def _is_bepinex_pack(self, version: PackageVersion) -> bool:
        return version.name.lower().startswith("bepinexpack")

    def _source_root(self, cached: Path, version: PackageVersion) -> tuple[Path, bool]:
        """Locate the real content root and whether it installs to the profile root.

        The BepInEx pack wraps everything in a ``BepInExPack_Valheim/`` folder
        whose contents belong at the profile root (``BepInEx/``, ``doorstop_libs/``,
        ...) rather than under the normal plugin routes.
        """
        if not self._is_bepinex_pack(version):
            return cached, False
        for child in sorted(cached.iterdir()):
            if child.is_dir() and child.name.lower().startswith("bepinexpack"):
                return child, True
        return cached, True

    def _install_files(
        self, version: PackageVersion, cached: Path
    ) -> tuple[list[str], list[str]]:
        source_root, to_profile_root = self._source_root(cached, version)
        mod_folder = version.package_full_name
        installed: list[str] = []
        configs: list[str] = []

        for source in sorted(source_root.rglob("*")):
            if not source.is_file() or source.name == ".vhsm-complete":
                continue
            relative = PurePosixPath(source.relative_to(source_root).as_posix())

            if to_profile_root:
                if rules.is_excluded(relative):
                    continue
                target_relative: PurePosixPath | None = relative
            else:
                target_relative = rules.resolve(relative, mod_folder)
            if target_relative is None:
                continue
            # Config is shared and operator-owned wherever it came from.
            preserve = rules.is_preserved(target_relative)

            target = self.layout.root / Path(target_relative)
            # Every destination is derived from archive contents, so re-check
            # it really landed inside the profile.
            if self.layout.root.resolve() not in target.resolve().parents:
                raise ModError(f"refusing to write outside the profile: {target_relative}")

            if preserve:
                # Record it either way so the manifest lists what the package
                # shipped, but never clobber settings already on disk.
                configs.append(str(target_relative))
                if target.exists():
                    continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if not preserve:
                installed.append(str(target_relative))

        if not installed and not configs:
            raise ModError(f"{version.full_name} contained no installable files")
        return installed, configs

    async def install(
        self,
        version: PackageVersion,
        *,
        progress: ProgressHook | None = None,
    ) -> list[InstalledMod]:
        """Install a package and everything it depends on."""

        def report(message: str) -> None:
            if progress:
                progress(message)

        chain = self._resolve_chain(version)
        newly_installed: list[InstalledMod] = []

        for candidate, is_dependency in chain:
            existing = self.get(candidate.package_full_name)
            if existing and existing.version == candidate.version_number:
                report(f"{candidate.full_name} already installed")
                continue
            if existing:
                report(f"replacing {existing.full_name} with {candidate.version_number}")
                self._remove_files(existing)
                self._mods.remove(existing)

            cached = await ensure_cached(self.cache_dir, candidate, progress)
            report(f"installing {candidate.full_name}")
            files, configs = self._install_files(candidate, cached)
            mod = InstalledMod(
                namespace=candidate.namespace,
                name=candidate.name,
                version=candidate.version_number,
                dependency_only=is_dependency and existing is None,
                description=candidate.description,
                icon=candidate.icon,
                website_url=candidate.website_url,
                dependencies=candidate.dependencies,
                files=files,
                config_files=configs,
            )
            # Keep an explicitly requested mod explicit even if it was first
            # pulled in as somebody else's dependency.
            if existing is not None and not existing.dependency_only:
                mod.dependency_only = False
            self._mods.append(mod)
            newly_installed.append(mod)

        self.save()
        return newly_installed

    async def install_by_name(
        self, package_full_name: str, version_number: str = "", *, progress: ProgressHook | None = None
    ) -> list[InstalledMod]:
        package = self.index.get(package_full_name)
        if package is None:
            raise ModError(f"{package_full_name} is not on Thunderstore")
        if version_number:
            target = next(
                (v for v in package.versions if v.version_number == version_number), None
            )
            if target is None:
                raise ModError(f"{package_full_name} has no version {version_number}")
        else:
            target = package.latest
        if target is None:
            raise ModError(f"{package_full_name} has no published versions")
        return await self.install(target, progress=progress)

    # ------------------------------------------------------------------ #
    # removing / toggling
    # ------------------------------------------------------------------ #
    def _remove_files(self, mod: InstalledMod) -> None:
        directories: set[Path] = set()
        for relative in mod.files:
            for candidate in (relative, relative + DISABLED_SUFFIX):
                path = self.layout.root / candidate
                try:
                    if path.is_file():
                        path.unlink()
                        directories.add(path.parent)
                except OSError:
                    continue
        # Prune directories the mod created, deepest first.
        for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
            current = directory
            while current != self.layout.root and current.is_dir():
                try:
                    current.rmdir()
                except OSError:
                    break
                current = current.parent

    def uninstall(self, package_full_name: str) -> InstalledMod:
        mod = self.get(package_full_name)
        if mod is None:
            raise ModError(f"{package_full_name} is not installed")
        self._remove_files(mod)
        self._mods.remove(mod)
        self.save()
        return mod

    def set_enabled(self, package_full_name: str, enabled: bool) -> InstalledMod:
        """Enable or disable by renaming files, keeping them on disk."""
        mod = self.get(package_full_name)
        if mod is None:
            raise ModError(f"{package_full_name} is not installed")
        if mod.enabled == enabled:
            return mod

        for relative in mod.files:
            active = self.layout.root / relative
            disabled = self.layout.root / (relative + DISABLED_SUFFIX)
            try:
                if enabled and disabled.is_file():
                    disabled.rename(active)
                elif not enabled and active.is_file():
                    active.rename(disabled)
            except OSError as exc:
                raise ModError(f"could not toggle {relative}: {exc}") from exc

        mod.enabled = enabled
        self.save()
        return mod

    def orphans(self) -> list[InstalledMod]:
        """Dependency-only mods nothing depends on any more."""
        return [
            mod
            for mod in self._mods
            if mod.dependency_only
            and mod.package_full_name.lower() != BEPINEX_PACKAGE.lower()
            and not self.dependents_of(mod.package_full_name)
        ]

    # ------------------------------------------------------------------ #
    # export / import
    # ------------------------------------------------------------------ #
    def export(self, profile_name: str) -> dict[str, Any]:
        """A portable description of this mod set, r2modman style."""
        return {
            "profileName": profile_name,
            "source": "vhsm",
            "exported_at": time.time(),
            "mods": [
                {
                    "name": mod.package_full_name,
                    "version": mod.version,
                    "enabled": mod.enabled,
                }
                for mod in self._mods
            ],
        }

    async def import_mods(
        self, payload: dict[str, Any], *, progress: ProgressHook | None = None
    ) -> list[InstalledMod]:
        entries = payload.get("mods")
        if not isinstance(entries, list):
            raise ModError("export file has no 'mods' list")

        installed: list[InstalledMod] = []
        disabled: list[str] = []
        for entry in entries:
            name = entry.get("name") or entry.get("full_name") or ""
            version = str(entry.get("version") or "")
            if isinstance(entry.get("version"), dict):
                # r2modman writes {major, minor, patch}.
                parts = entry["version"]
                version = ".".join(
                    str(parts.get(k, 0)) for k in ("major", "minor", "patch")
                )
            if not name:
                continue
            try:
                installed += await self.install_by_name(name, version, progress=progress)
            except (ModError, ThunderstoreError) as exc:
                if progress:
                    progress(f"skipped {name}: {exc}")
                continue
            if entry.get("enabled") is False:
                disabled.append(name)

        for name in disabled:
            try:
                self.set_enabled(name, False)
            except ModError:
                continue
        return installed

    # ------------------------------------------------------------------ #
    def summary(self) -> dict[str, Any]:
        return {
            "count": len(self._mods),
            "enabled": sum(1 for m in self._mods if m.enabled),
            "bepinex_installed": self.get(BEPINEX_PACKAGE) is not None,
            "mods": [m.to_dict() for m in self._mods],
            "updates": self.updates_available(),
        }
