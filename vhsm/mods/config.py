"""Reading and writing a mod's BepInEx configuration.

BepInEx writes one ``.cfg`` file per plugin into ``BepInEx/config/``, and the
format carries more than key/value pairs: every entry is preceded by its
description, its .NET type, its default value and -- when the plugin declared
one -- the values or the range it will accept::

    ## Hit points a boar spawns with
    # Setting type: Int32
    # Default value: 10
    # Acceptable value range: From 1 to 500
    BoarHealth = 25

That is enough to build a real form instead of asking an operator to edit a
text file over SSH, which is exactly what r2modman's config editor does on the
desktop. This module is the parsing half: it turns a file into typed entries,
validates what comes back from the form, and writes the result out *by
replacing single lines*, so comments, blank lines, ordering and any key we did
not understand survive untouched.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from ..instance import InstanceLayout
from ..util import safe_relative
from .profile import InstalledMod

#: Files we are willing to open in the editor. Everything BepInEx and the mods
#: themselves keep in the config tree is text; anything else is left alone.
EDITABLE_SUFFIXES = {
    ".cfg", ".json", ".txt", ".yml", ".yaml", ".ini", ".toml", ".xml", ".md", ".csv",
}
#: Only ``.cfg`` gets the structured treatment; the rest are raw text.
STRUCTURED_SUFFIX = ".cfg"
#: A config file large enough to hang the browser is better left to an editor.
MAX_EDITABLE_BYTES = 2 * 1024 * 1024
#: Shortest name that may be matched to a mod by prefix alone.
MIN_PREFIX_MATCH = 6

#: .NET type names BepInEx writes for whole numbers, and for fractional ones.
INTEGER_TYPES = {
    "byte", "sbyte", "int16", "uint16", "int32", "uint32", "int64", "uint64",
    "short", "ushort", "int", "uint", "long", "ulong",
}
DECIMAL_TYPES = {"single", "double", "decimal", "float"}
BOOLEAN_TYPES = {"boolean", "bool"}

_SECTION_RE = re.compile(r"^\s*\[(?P<name>.+)\]\s*$")
_ENTRY_RE = re.compile(r"^(?P<indent>\s*)(?P<key>[^#\[\s][^=]*?)\s*=\s?(?P<value>.*)$")
_META_RE = re.compile(r"^\s*#\s*(?P<key>[A-Za-z][A-Za-z ]*?)\s*:\s*(?P<value>.*)$")
_RANGE_RE = re.compile(r"^From\s+(?P<low>\S+)\s+to\s+(?P<high>\S+)\s*$", re.IGNORECASE)
#: BepInEx writes this line under a [Flags] enum, and it has no ``Key: value``
#: shape, so it is matched on its own rather than as metadata.
_FLAGS_RE = re.compile(r"multiple values can be set at the same time", re.IGNORECASE)
_PLUGIN_RE = re.compile(
    r"^##\s*Settings file was created by plugin\s+(?P<name>.+?)\s+v(?P<version>[0-9][^\s]*)\s*$"
)
_GUID_RE = re.compile(r"^##\s*Plugin GUID:\s*(?P<guid>.+?)\s*$")
_NORMALISE_RE = re.compile(r"[^a-z0-9]+")


class ConfigError(ValueError):
    """A value the config file would not accept."""


def _normalise(value: str) -> str:
    """Squash a name to comparable letters and digits."""
    return _NORMALISE_RE.sub("", value.lower())


def _tokens(value: str) -> set[str]:
    """The words in a dotted name, normalised.

    ``com.jotunn.jotunn`` is the usual shape of a plugin GUID and of the file
    named after it, and the mod's name is one of its words. Splitting first
    means matching a whole word rather than any substring, so a short mod name
    cannot attach itself to an unrelated file.
    """
    return {_normalise(part) for part in re.split(r"[^A-Za-z0-9]+", value) if part}


# --------------------------------------------------------------------------- #
# entries
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class ConfigEntry:
    """One ``key = value`` line, with everything BepInEx said about it."""

    section: str
    key: str
    value: str
    line: int
    default: str = ""
    description: str = ""
    setting_type: str = ""
    options: list[str] = field(default_factory=list)
    minimum: str = ""
    maximum: str = ""
    #: True for a flags enum, where several acceptable values may be combined.
    multiple: bool = False
    has_default: bool = False
    indent: str = ""

    # -- presentation ------------------------------------------------- #
    @property
    def kind(self) -> str:
        """Which widget this entry wants: the whole point of the metadata."""
        lowered = self.setting_type.lower()
        if lowered in BOOLEAN_TYPES:
            return "bool"
        if self.options:
            return "flags" if self.multiple else "enum"
        if lowered in INTEGER_TYPES:
            return "int"
        if lowered in DECIMAL_TYPES:
            return "float"
        return "text"

    @property
    def numeric(self) -> bool:
        return self.kind in ("int", "float")

    @property
    def step(self) -> str:
        """A sane HTML ``step`` so the spinner does not fight a float."""
        return "1" if self.kind == "int" else "any"

    @property
    def selected(self) -> list[str]:
        """The currently set options of a flags entry."""
        return [part.strip() for part in self.value.split(",") if part.strip()]

    @property
    def is_default(self) -> bool:
        if not self.has_default:
            return True
        if self.kind == "flags":
            # A flags entry is a set: ``Info, Warning`` and ``Warning, Info``
            # are the same setting, and neither is a change worth flagging.
            return self._flag_set(self.value) == self._flag_set(self.default)
        return _same_value(self.value, self.default)

    @staticmethod
    def _flag_set(value: str) -> set[str]:
        return {part.strip().lower() for part in value.split(",") if part.strip()}

    @property
    def bounds_note(self) -> str:
        if self.minimum or self.maximum:
            return f"{self.minimum} – {self.maximum}"
        return ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "key": self.key,
            "value": self.value,
            "default": self.default,
            "description": self.description,
            "setting_type": self.setting_type,
            "options": list(self.options),
            "minimum": self.minimum,
            "maximum": self.maximum,
            "kind": self.kind,
            "is_default": self.is_default,
        }


def _same_value(left: str, right: str) -> bool:
    """Compare two config values the way the file means them.

    ``1`` and ``1.0`` are the same float, ``True`` and ``true`` the same
    boolean; only after those does a plain string comparison make sense.
    """
    a, b = left.strip(), right.strip()
    if a.lower() == b.lower():
        return True
    try:
        return float(a) == float(b)
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def _parse_number(raw: str, entry: ConfigEntry) -> float:
    try:
        return int(raw) if entry.kind == "int" else float(raw)
    except ValueError:
        word = "whole number" if entry.kind == "int" else "number"
        raise ConfigError(f"{entry.key} must be a {word}, not {raw!r}") from None


def coerce(entry: ConfigEntry, raw: str) -> str:
    """Return *raw* in the spelling the file wants, or explain why it cannot.

    Values are checked against the same metadata BepInEx wrote, so a rejected
    value here is one the plugin would have rejected (or silently reset) at
    boot -- which is the failure this editor exists to prevent.
    """
    value = raw.strip()
    kind = entry.kind

    if kind == "bool":
        lowered = value.lower()
        if lowered in ("true", "1", "on", "yes"):
            return "true"
        if lowered in ("false", "0", "off", "no", ""):
            return "false"
        raise ConfigError(f"{entry.key} must be true or false, not {raw!r}")

    if kind in ("enum", "flags"):
        canonical = {option.lower(): option for option in entry.options}
        parts = [p.strip() for p in value.split(",") if p.strip()] if kind == "flags" else [value]
        chosen: list[str] = []
        for part in parts:
            match = canonical.get(part.lower())
            if match is None:
                allowed = ", ".join(entry.options)
                raise ConfigError(f"{entry.key} must be one of: {allowed} (got {part!r})")
            if match not in chosen:
                chosen.append(match)
        if kind == "flags":
            # Ordered by the plugin's own list rather than by the order the
            # boxes happened to be ticked, so saving twice is idempotent.
            return ", ".join(o for o in entry.options if o in chosen)
        return chosen[0] if chosen else ""

    if kind in ("int", "float"):
        number = _parse_number(value, entry)
        if entry.minimum:
            low = _parse_number(entry.minimum, entry)
            if number < low:
                raise ConfigError(f"{entry.key} cannot be below {entry.minimum}")
        if entry.maximum:
            high = _parse_number(entry.maximum, entry)
            if number > high:
                raise ConfigError(f"{entry.key} cannot be above {entry.maximum}")
        return value

    # Plain strings: a newline would silently swallow the rest of the entry,
    # so fold one to a space rather than corrupting the file.
    return value.replace("\r", "").replace("\n", " ")


# --------------------------------------------------------------------------- #
# the document
# --------------------------------------------------------------------------- #
class ConfigDocument:
    """A parsed ``.cfg``: typed entries over the original lines.

    The lines are the source of truth. Editing an entry rewrites exactly one
    of them, so a file round-trips byte for byte when nothing changed.
    """

    def __init__(self, text: str) -> None:
        self.newline = "\r\n" if "\r\n" in text else "\n"
        self.trailing_newline = text.endswith(("\n", "\r"))
        self.lines: list[str] = text.replace("\r\n", "\n").split("\n")
        if self.trailing_newline and self.lines and self.lines[-1] == "":
            self.lines.pop()
        self.entries: list[ConfigEntry] = []
        self.plugin_name = ""
        self.plugin_version = ""
        self.plugin_guid = ""
        self._parse()

    # -- parsing -------------------------------------------------------- #
    def _parse(self) -> None:
        section = ""
        description: list[str] = []
        meta: dict[str, str] = {}

        for number, line in enumerate(self.lines):
            stripped = line.strip()

            if not stripped:
                continue

            if stripped.startswith("#"):
                plugin = _PLUGIN_RE.match(stripped)
                if plugin and not self.plugin_name:
                    self.plugin_name = plugin.group("name")
                    self.plugin_version = plugin.group("version")
                    continue
                guid = _GUID_RE.match(stripped)
                if guid and not self.plugin_guid:
                    self.plugin_guid = guid.group("guid")
                    continue
                if _FLAGS_RE.search(stripped):
                    meta["flags"] = "yes"
                    continue
                if stripped.startswith("##"):
                    description.append(stripped[2:].strip())
                    continue
                found = _META_RE.match(stripped)
                if found:
                    meta[found.group("key").strip().lower()] = found.group("value").strip()
                else:
                    # A bare ``#`` comment reads as description too.
                    description.append(stripped[1:].strip())
                continue

            header = _SECTION_RE.match(line)
            if header:
                section = header.group("name").strip()
                description, meta = [], {}
                continue

            entry = _ENTRY_RE.match(line)
            if entry:
                self.entries.append(
                    _build_entry(section, entry, number, description, meta)
                )
            description, meta = [], {}

    # -- queries -------------------------------------------------------- #
    @property
    def structured(self) -> bool:
        return bool(self.entries)

    @property
    def numbered_sections(self) -> list[tuple[str, list[tuple[int, ConfigEntry]]]]:
        """``sections``, but each entry carries its index in the document.

        The form names its fields after that index, so the template needs it
        while it is still walking the entries section by section.
        """
        numbered: dict[str, list[tuple[int, ConfigEntry]]] = {}
        for number, entry in enumerate(self.entries):
            numbered.setdefault(entry.section, []).append((number, entry))
        return list(numbered.items())

    @property
    def modified_count(self) -> int:
        return sum(1 for entry in self.entries if not entry.is_default)

    def get(self, section: str, key: str) -> ConfigEntry | None:
        for entry in self.entries:
            if entry.section == section and entry.key == key:
                return entry
        return None

    @property
    def text(self) -> str:
        body = self.newline.join(self.lines)
        return body + self.newline if self.trailing_newline else body

    # -- mutation ------------------------------------------------------- #
    def set_value(self, entry: ConfigEntry, raw: str) -> bool:
        """Validate and apply one value. True when the file actually changed."""
        value = coerce(entry, raw)
        if value == entry.value:
            return False
        entry.value = value
        self.lines[entry.line] = f"{entry.indent}{entry.key} = {value}"
        return True

    def apply(self, values: dict[tuple[str, str], str]) -> tuple[int, list[str]]:
        """Apply a form's worth of values.

        Returns the number of changed entries and a message per rejected one.
        A bad value never blocks the good ones: the operator keeps the rest of
        their edits and is told exactly which field kept its old value.
        """
        changed = 0
        problems: list[str] = []
        for (section, key), raw in values.items():
            entry = self.get(section, key)
            if entry is None:
                continue
            try:
                changed += 1 if self.set_value(entry, raw) else 0
            except ConfigError as exc:
                label = f"[{section}] " if section else ""
                problems.append(f"{label}{exc}")
        return changed, problems

    def reset_to_defaults(self) -> int:
        """Put every entry back to the default the plugin shipped."""
        changed = 0
        for entry in self.entries:
            if not entry.has_default or entry.is_default:
                continue
            try:
                changed += 1 if self.set_value(entry, entry.default) else 0
            except ConfigError:
                continue
        return changed

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin_name": self.plugin_name,
            "plugin_version": self.plugin_version,
            "plugin_guid": self.plugin_guid,
            "structured": self.structured,
            "modified": self.modified_count,
            "entries": [entry.to_dict() for entry in self.entries],
        }


def _build_entry(
    section: str,
    match: "re.Match[str]",
    number: int,
    description: list[str],
    meta: dict[str, str],
) -> ConfigEntry:
    options: list[str] = []
    minimum = maximum = ""

    acceptable = meta.get("acceptable values", "")
    if acceptable:
        options = [part.strip() for part in acceptable.split(",") if part.strip()]
    value_range = meta.get("acceptable value range", "")
    if value_range:
        bounds = _RANGE_RE.match(value_range)
        if bounds:
            minimum, maximum = bounds.group("low"), bounds.group("high")

    default = meta.get("default value", "")
    return ConfigEntry(
        section=section,
        key=match.group("key").strip(),
        value=match.group("value").strip(),
        line=number,
        default=default,
        has_default="default value" in meta,
        description=" ".join(d for d in description if d).strip(),
        setting_type=meta.get("setting type", ""),
        options=options,
        minimum=minimum,
        maximum=maximum,
        # A [Flags] enum: BepInEx says so in a comment, and its default
        # value is the list of flags that start out set.
        multiple=bool(meta.get("flags")) or bool(options and "," in default),
        indent=match.group("indent"),
    )


# --------------------------------------------------------------------------- #
# files on disk
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class ConfigFile:
    """One file in the profile's config tree, and who it belongs to."""

    relative: str
    path: Path
    size: int
    modified: float
    owner: str = ""
    owner_name: str = ""
    owner_icon: str = ""
    plugin_name: str = ""
    plugin_guid: str = ""
    entry_count: int = 0
    modified_count: int = 0
    structured: bool = False

    @property
    def name(self) -> str:
        return PurePosixPath(self.relative).name

    @property
    def label(self) -> str:
        """What to call the file in a list: the plugin's own name if it gave one."""
        return self.plugin_name or PurePosixPath(self.relative).stem

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative": self.relative,
            "name": self.name,
            "label": self.label,
            "size": self.size,
            "modified": self.modified,
            "owner": self.owner,
            "owner_name": self.owner_name,
            "plugin_name": self.plugin_name,
            "plugin_guid": self.plugin_guid,
            "entries": self.entry_count,
            "modified_entries": self.modified_count,
            "structured": self.structured,
        }


def config_root(layout: InstanceLayout) -> Path:
    return layout.bepinex / "config"


def resolve_config_path(layout: InstanceLayout, relative: str) -> Path:
    """Resolve an operator-supplied path inside the config tree, or refuse.

    The path arrives from a query string, so it is guarded the same way every
    other externally-influenced path in the manager is.
    """
    cleaned = (relative or "").strip().replace("\\", "/").lstrip("/")
    if not cleaned:
        raise ConfigError("no config file given")
    root = config_root(layout)
    try:
        path = safe_relative(root, Path(cleaned))
    except ValueError as exc:
        raise ConfigError("that path is outside the config folder") from exc
    if path == root:
        raise ConfigError("that is the config folder, not a file")
    if path.suffix.lower() not in EDITABLE_SUFFIXES:
        raise ConfigError(f"{path.name} is not a text config file")
    return path


def read_document(path: Path) -> ConfigDocument:
    if not path.is_file():
        raise ConfigError(f"{path.name} does not exist")
    if path.stat().st_size > MAX_EDITABLE_BYTES:
        raise ConfigError(f"{path.name} is too large to edit here")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path.name} is not UTF-8 text") from exc
    return ConfigDocument(text)


def write_text(path: Path, text: str) -> None:
    """Replace a config file atomically.

    A half-written config is worse than a stale one: BepInEx would read it at
    the next boot and quietly fall back to defaults for everything after the
    truncation point.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as sink:
            sink.write(text)
            sink.flush()
            os.fsync(sink.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def normalise_submitted(text: str, document: ConfigDocument | None = None) -> str:
    """Take a textarea's contents back to the file's own line endings."""
    body = text.replace("\r\n", "\n").replace("\r", "\n")
    newline = document.newline if document else "\n"
    if not body.endswith("\n"):
        body += "\n"
    return body.replace("\n", newline) if newline != "\n" else body


# --------------------------------------------------------------------------- #
# attribution
# --------------------------------------------------------------------------- #
def _mod_keys(mod: InstalledMod) -> set[str]:
    """Names a config file might plausibly be called after this mod."""
    keys = {
        _normalise(mod.name),
        _normalise(mod.package_full_name),
        _normalise(f"{mod.namespace}{mod.name}"),
    }
    return {key for key in keys if key}


def _owner_from_manifest(mods: Iterable[InstalledMod]) -> dict[str, InstalledMod]:
    """Config paths a package shipped, mapped to the mod that shipped them."""
    claimed: dict[str, InstalledMod] = {}
    for mod in mods:
        for tracked in mod.config_files:
            posix = PurePosixPath(tracked)
            try:
                inside = posix.relative_to("BepInEx/config")
            except ValueError:
                continue
            claimed.setdefault(str(inside).lower(), mod)
    return claimed


def attribute(
    file: ConfigFile, mods: list[InstalledMod], claimed: dict[str, InstalledMod]
) -> InstalledMod | None:
    """Work out which installed mod a config file belongs to.

    In order of how much the evidence is worth: the package said it shipped
    this file; the plugin stamped its GUID or name into the header; or the
    filename simply matches the mod's. BepInEx generates most config files at
    runtime, so the header is usually what settles it.
    """
    direct = claimed.get(file.relative.lower())
    if direct is not None:
        return direct

    guid = _normalise(file.plugin_guid)
    plugin = _normalise(file.plugin_name)
    stem = _normalise(PurePosixPath(file.relative).stem)

    if plugin:
        for mod in mods:
            if plugin in _mod_keys(mod):
                return mod
    if stem:
        for mod in mods:
            if stem in _mod_keys(mod):
                return mod
    # A whole word inside a dotted name: ``com.jotunn.jotunn.cfg``, which is
    # what a mod that names its config after its GUID leaves behind.
    words = _tokens(file.plugin_guid) | _tokens(PurePosixPath(file.relative).stem)
    if words:
        for mod in mods:
            if words & _mod_keys(mod):
                return mod
    if guid:
        # Looser: a GUID such as ``org.bepinex.plugins.valheim_plus`` holds the
        # mod's name split across words, so only the squashed form matches.
        for mod in mods:
            if any(key in guid for key in _mod_keys(mod)):
                return mod
    # Loosest: one name is the start of the other, which is how
    # ``BepInEx.cfg`` finds ``BepInExPack_Valheim``. Short names are left out,
    # because at three or four letters this stops being evidence.
    for candidate in (plugin, stem, guid):
        if len(candidate) < MIN_PREFIX_MATCH:
            continue
        for mod in mods:
            if any(key.startswith(candidate) or candidate.startswith(key) for key in _mod_keys(mod)):
                return mod
    return None


@dataclass(slots=True)
class ConfigGroup:
    """Config files belonging to one mod (or to nothing installed)."""

    owner: str
    name: str
    icon: str = ""
    enabled: bool = True
    files: list[ConfigFile] = field(default_factory=list)


def scan(layout: InstanceLayout, mods: list[InstalledMod]) -> list[ConfigFile]:
    """Every editable file in the profile's config tree, attributed to a mod."""
    root = config_root(layout)
    if not root.is_dir():
        return []

    claimed = _owner_from_manifest(mods)
    files: list[ConfigFile] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in EDITABLE_SUFFIXES:
            continue
        stat = path.stat()
        entry = ConfigFile(
            relative=path.relative_to(root).as_posix(),
            path=path,
            size=stat.st_size,
            modified=stat.st_mtime,
        )
        if path.suffix.lower() == STRUCTURED_SUFFIX and stat.st_size <= MAX_EDITABLE_BYTES:
            try:
                document = read_document(path)
            except ConfigError:
                document = None
            if document is not None:
                entry.plugin_name = document.plugin_name
                entry.plugin_guid = document.plugin_guid
                entry.entry_count = len(document.entries)
                entry.modified_count = document.modified_count
                entry.structured = document.structured

        owner = attribute(entry, mods, claimed)
        if owner is not None:
            entry.owner = owner.package_full_name
            entry.owner_name = owner.name
            entry.owner_icon = owner.icon
        files.append(entry)
    return files


def _rank(file: ConfigFile) -> tuple:
    """Sort key putting a mod's main config file first.

    A mod can own several files -- one the plugin writes and one it shipped,
    say -- and the Config button has to open one of them. The parseable file
    with the most settings in it is the one the operator came for.
    """
    return (not file.structured, -file.entry_count, file.relative.lower())


def index_by_owner(files: list[ConfigFile]) -> dict[str, list[ConfigFile]]:
    """Config files keyed by the package that owns them, best file first."""
    index: dict[str, list[ConfigFile]] = {}
    for file in files:
        index.setdefault(file.owner, []).append(file)
    for owned in index.values():
        owned.sort(key=_rank)
    return index


def group_by_mod(files: list[ConfigFile], mods: list[InstalledMod]) -> list[ConfigGroup]:
    """Files grouped for display: one group per mod, installed mods first.

    Mods with no config file still get a group, because "this mod has no
    config yet" is the answer to a question the operator is about to ask.
    """
    by_owner = index_by_owner(files)

    groups: list[ConfigGroup] = []
    for mod in sorted(mods, key=lambda m: (m.dependency_only, m.name.lower())):
        groups.append(
            ConfigGroup(
                owner=mod.package_full_name,
                name=mod.name,
                icon=mod.icon,
                enabled=mod.enabled,
                files=by_owner.pop(mod.package_full_name, []),
            )
        )
    # Anything left over belongs to a plugin we cannot tie to an installed
    # package -- a hand-dropped dll, or a mod removed after it wrote config.
    leftovers = by_owner.pop("", [])
    for remaining in by_owner.values():
        leftovers += remaining
    leftovers.sort(key=_rank)
    if leftovers:
        groups.append(ConfigGroup(owner="", name="Unmatched files", files=leftovers))
    return groups


def summary(layout: InstanceLayout, mods: list[InstalledMod]) -> dict[str, Any]:
    files = scan(layout, mods)
    return {
        "count": len(files),
        "files": [f.to_dict() for f in files],
        "root": str(config_root(layout)),
    }
