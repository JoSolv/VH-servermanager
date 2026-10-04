"""A mod profile: the set of mods installed into one instance.

Modelled on r2modman. The instance directory *is* the profile -- mods are
materialised into ``<instance>/BepInEx`` and recorded in ``mods.json`` with the
exact list of files each one owns, so uninstalling is precise rather than a
guess, and disabling is reversible.
"""

from __future__ import annotations

import io
import shutil
import time
import zipfile
from dataclasses import dataclass, field, asdict
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import yaml

from ..instance import InstanceLayout
from ..util import read_json, write_json
from . import rules
from .bepinex import BEPINEX_PACKAGE
from .cache import MAX_PACKAGE_BYTES, ensure_cached
from .thunderstore import (
    PackageVersion,
    ThunderstoreError,
    ThunderstoreIndex,
    is_newer,
    parse_dependency,
)

MANIFEST_VERSION = 1
#: Suffix used to disable a file without deleting it, as r2modman does.
DISABLED_SUFFIX = ".old"
#: Guard against a malformed dependency graph.
MAX_DEPENDENCY_DEPTH = 24

#: The mod list inside an r2modman profile (``.r2z``), in YAML.
R2X_NAME = "export.r2x"
#: A mod list is a few kilobytes; one this large is not a mod list.
MAX_R2X_BYTES = 1024 * 1024
#: The file types r2modman exports from outside ``config/``
#: (``ProfileModList.SUPPORTED_CONFIG_FILE_EXTENSIONS``), and so the only ones
#: an import takes from there.
CONFIG_EXTENSIONS = (".cfg", ".txt", ".json", ".yml", ".yaml", ".ini")
#: Never unpacked from ``config/`` -- the list r2modman's own import refuses.
BLOCKED_EXTENSIONS = (
    ".dll", ".exe", ".scr", ".com", ".pif", ".bat", ".cmd", ".ps1", ".vbs", ".vbe",
    ".js", ".jse", ".wsf", ".wsh", ".hta", ".msi", ".msix", ".sys", ".drv", ".cpl",
    ".ocx", ".lnk", ".reg", ".inf",
)
#: What :mod:`zipfile` can decompress. Anything else, or an encrypted member,
#: would only fail while unpacking -- after the mods had been replaced.
READABLE_COMPRESSION = (
    zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA,
)

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


@dataclass(slots=True)
class ExportedMod:
    """One line of an exported mod list."""

    package_full_name: str
    version: str
    enabled: bool


@dataclass(slots=True)
class ProfileExport:
    """An uploaded profile, read and checked before anything is changed."""

    mods: list[ExportedMod]
    #: The ``.r2z`` the files come from; ``None`` for a bare mod list.
    archive: zipfile.ZipFile | None = None
    #: Archive members to unpack, each with the profile-relative path it goes to.
    files: list[tuple[zipfile.ZipInfo, PurePosixPath]] = field(default_factory=list)
    #: Archive members left out because they are not BepInEx configuration.
    left_out: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ImportResult:
    installed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    #: Listed mods that could not be had, as ``namespace-name-version``.
    missing: list[str] = field(default_factory=list)


def read_export(raw: bytes) -> ProfileExport:
    """Read an uploaded profile: an r2modman ``.r2z`` or ``.r2x``, or the JSON
    mod list this manager exported before it wrote ``.r2z``.

    Every path in a ``.r2z`` is checked here, so a bad archive is refused
    before the profile is touched. Of its files only BepInEx configuration is
    taken: ``config/`` goes to ``BepInEx/config``, where r2modman took it
    from, minus executables; elsewhere under ``BepInEx/`` only config-type
    files. Anything else -- r2modman's copy of ``doorstop_config.ini``, or a
    file aimed at the instance directory, where ``instance.json`` and the
    admin list live -- is left out.
    """
    archive: zipfile.ZipFile | None = None
    data = raw
    if zipfile.is_zipfile(io.BytesIO(raw)):
        try:
            archive = zipfile.ZipFile(io.BytesIO(raw))
            info = archive.getinfo(R2X_NAME)
        except zipfile.BadZipFile as exc:
            raise ModError(f"not a readable zip file: {exc}") from exc
        except KeyError:
            raise ModError(f"there is no {R2X_NAME} in it, so it is not an r2modman profile") from None
        if info.file_size > MAX_R2X_BYTES:
            raise ModError(f"its {R2X_NAME} is far too large to be a mod list")
        if not _readable(info):
            raise ModError(f"its {R2X_NAME} is encrypted or compressed in a way that cannot be read")
        data = archive.read(info)

    # YAML reads JSON as well, which covers the older exports.
    try:
        payload = yaml.safe_load(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ModError(f"the mod list is not readable YAML or JSON: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("mods"), list):
        raise ModError("there is no list of mods in it")

    mods: list[ExportedMod] = []
    for entry in payload["mods"]:
        name = entry.get("name") if isinstance(entry, dict) else None
        version = entry.get("version") if isinstance(entry, dict) else None
        if isinstance(version, dict):
            # r2modman writes {major, minor, patch}.
            version = ".".join(str(version.get(k, 0)) for k in ("major", "minor", "patch"))
        if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
            raise ModError(f"a mod entry has no name or version: {entry!r}")
        # r2modman reads a missing flag as enabled.
        mods.append(ExportedMod(name, version, bool(entry.get("enabled", True))))

    export = ProfileExport(mods=mods, archive=archive)
    if archive is None:
        return export
    total = 0
    for info in archive.infolist():
        name = info.filename
        if info.is_dir() or name == R2X_NAME:
            continue
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or name.startswith("\\"):
            raise ModError(f"unsafe path in the profile: {name}")
        top, lower = relative.parts[0], name.lower()
        if top == "config" and len(relative.parts) > 1 and not lower.endswith(BLOCKED_EXTENSIONS):
            export.files.append((info, PurePosixPath("BepInEx") / relative))
        elif top == "BepInEx" and len(relative.parts) > 1 and lower.endswith(CONFIG_EXTENSIONS):
            export.files.append((info, relative))
        else:
            export.left_out.append(name)
            continue
        if not _readable(info):
            raise ModError(f"{name} is encrypted or compressed in a way that cannot be read")
        total += info.file_size
        if total > MAX_PACKAGE_BYTES:
            raise ModError("the profile's files are unreasonably large")
    return export


def _readable(info: zipfile.ZipInfo) -> bool:
    return not info.flag_bits & 0x1 and info.compress_type in READABLE_COMPRESSION


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
        """Installed mods that Thunderstore has a newer version of."""
        updates = []
        for mod in self._mods:
            package = self.index.get(mod.package_full_name)
            latest = package.latest if package else None
            if latest and is_newer(latest.version_number, mod.version):
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
        """Flatten a package and the dependencies it still needs, dependencies first.

        The boolean is ``dependency_only``. BepInEx is appended implicitly
        because a Valheim mod is unloadable without it and not every package
        bothers to declare it.

        Dependencies follow r2modman: one that is already installed is left
        at whatever version it is, and a missing one comes in at its latest
        version. The version in a dependency string is the build the author
        compiled against, not a pin -- taking it literally replaced a newer
        shared library, which every other mod here was using, with an older one.
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
                namespace, name, _ = parse_dependency(dependency)
                if self.get(f"{namespace}-{name}") is not None:
                    continue
                package = self.index.get(f"{namespace}-{name}")
                if package is None or package.latest is None:
                    raise ModError(
                        f"{candidate.full_name} needs {dependency}, which is not on Thunderstore"
                    )
                walk(package.latest, depth + 1, True)
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

            cached = await ensure_cached(self.cache_dir, candidate, progress)
            report(f"installing {candidate.full_name}")
            # Keep an explicitly requested mod explicit even if it was first
            # pulled in as somebody else's dependency.
            newly_installed.append(
                self._install_version(
                    candidate, cached, dependency_only=is_dependency and existing is None
                )
            )

        self.save()
        return newly_installed

    def _install_version(
        self, version: PackageVersion, cached: Path, *, dependency_only: bool
    ) -> InstalledMod:
        """Install *version* from the cache in place of any installed version of it."""
        existing = self.get(version.package_full_name)
        if existing:
            self._remove_files(existing)
            self._mods.remove(existing)
        files, configs = self._install_files(version, cached)
        mod = InstalledMod(
            namespace=version.namespace,
            name=version.name,
            version=version.version_number,
            dependency_only=dependency_only,
            description=version.description,
            icon=version.icon,
            website_url=version.website_url,
            dependencies=version.dependencies,
            files=files,
            config_files=configs,
        )
        self._mods.append(mod)
        return mod

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
    # export / import, as r2modman's .r2z
    # ------------------------------------------------------------------ #
    def export_r2z(self, profile_name: str, destination: Path) -> None:
        """Write this profile to *destination* as an r2modman ``.r2z``.

        The shape r2modman's own export has, so r2modman imports it:
        ``export.r2x`` listing the mods, ``config/`` holding ``BepInEx/config``,
        and the config-type files from the rest of ``BepInEx/``. Nothing outside
        ``BepInEx/`` is read -- the instance directory around it holds the
        server password (``instance.json``) and the admin and ban lists.

        A mod whose version is not ``major.minor.patch`` cannot be written in
        r2modman's format and is left out. Only a hand-uploaded zip with an odd
        filename gets such a version, and r2modman could not download it anyway.
        """
        mods = []
        for mod in self._mods:
            try:
                major, minor, patch = (int(part) for part in mod.version.split("."))
            except ValueError:
                continue
            mods.append(
                {
                    "name": mod.package_full_name,
                    "version": {"major": major, "minor": minor, "patch": patch},
                    "enabled": mod.enabled,
                }
            )

        config = self.layout.bepinex / "config"
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                R2X_NAME,
                yaml.safe_dump({"profileName": profile_name, "mods": mods}, sort_keys=False),
            )
            for path in sorted(self.layout.bepinex.rglob("*")):
                if not path.is_file():
                    continue
                if path.is_relative_to(config):
                    archive.write(path, "config/" + path.relative_to(config).as_posix())
                elif path.name.lower().endswith(CONFIG_EXTENSIONS):
                    archive.write(path, path.relative_to(self.layout.root).as_posix())

    async def replace_from_export(
        self, export: ProfileExport, *, progress: ProgressHook | None = None
    ) -> ImportResult:
        """Make this profile the one in *export*, as r2modman's import does.

        Afterwards the installed mods are exactly the ones the export lists,
        at the versions it lists, and anything else is removed. There is no
        dependency resolution: an export already names every dependency at the
        version it ran with. A listed version the catalogue does not have --
        withdrawn, never on Thunderstore, or published since the catalogue was
        last refreshed -- is skipped and reported, unless it is already
        installed here.

        Everything to be installed is downloaded before anything changes, so
        a failed download leaves the profile as it was. The export's files are
        then unpacked over what is here, config included.
        """
        keep: list[ExportedMod] = []
        fetch: list[tuple[ExportedMod, PackageVersion]] = []
        missing: list[str] = []
        for entry in export.mods:
            installed = self.get(entry.package_full_name)
            if installed and installed.version == entry.version:
                keep.append(entry)
                continue
            package = self.index.get(entry.package_full_name)
            version = next(
                (v for v in package.versions if v.version_number == entry.version), None
            ) if package else None
            if version is None:
                missing.append(f"{entry.package_full_name}-{entry.version}")
            else:
                fetch.append((entry, version))
        if export.mods and not keep and not fetch:
            raise ModError(
                "none of the mods in this profile are on Valheim's Thunderstore, "
                "so importing it would only remove this server's mods"
            )
        cached: list[tuple[PackageVersion, Path]] = []
        for _, version in fetch:
            cached.append((version, await ensure_cached(self.cache_dir, version, progress)))

        wanted = {entry.package_full_name.lower(): entry for entry in keep}
        wanted.update((entry.package_full_name.lower(), entry) for entry, _ in fetch)
        # What this server already knew about each mod, which an export does not say.
        known = {mod.package_full_name.lower(): mod.dependency_only for mod in self._mods}
        result = ImportResult(missing=missing)
        try:
            for mod in list(self._mods):
                if mod.package_full_name.lower() not in wanted:
                    self._remove_files(mod)
                    self._mods.remove(mod)
                    result.removed.append(mod.full_name)

            new = [
                self._install_version(version, path, dependency_only=False)
                for version, path in cached
            ]
            for mod in new:
                key = mod.package_full_name.lower()
                mod.dependency_only = (
                    known[key] if key in known else bool(self.dependents_of(mod.package_full_name))
                )
            result.installed = [mod.full_name for mod in new]

            # Unpack with every mod enabled, so a file lands on the mod's active
            # copy rather than beside a disabled one that enabling would rename
            # over it.
            for mod in self._mods:
                if not mod.enabled:
                    self.set_enabled(mod.package_full_name, True)
            root = self.layout.root.resolve()
            for info, relative in export.files:
                target = self.layout.root / Path(relative)
                if root not in target.resolve().parents:
                    raise ModError(f"refusing to write outside the profile: {relative}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with export.archive.open(info) as source, target.open("wb") as sink:
                    shutil.copyfileobj(source, sink)
            for mod in self._mods:
                if not wanted[mod.package_full_name.lower()].enabled:
                    self.set_enabled(mod.package_full_name, False)
        except (ModError, OSError, zipfile.BadZipFile) as exc:
            raise ModError(
                f"the import stopped part-way, so this server has only some of the profile: {exc}"
            ) from exc
        finally:
            self.save()
        return result

    # ------------------------------------------------------------------ #
    def summary(self) -> dict[str, Any]:
        return {
            "count": len(self._mods),
            "enabled": sum(1 for m in self._mods if m.enabled),
            "bepinex_installed": self.get(BEPINEX_PACKAGE) is not None,
            "mods": [m.to_dict() for m in self._mods],
            "updates": self.updates_available(),
        }
