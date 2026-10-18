"""Instance configuration model and the directory layout of one instance."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from .config import Settings
from .discord.config import DiscordConfigError, InstanceDiscord
from .util import normalise_time_of_day, slugify

#: Valheim world modifiers accepted by ``-modifier <key> <value>``.
MODIFIER_KEYS: dict[str, list[str]] = {
    "combat": ["veryeasy", "easy", "hard", "veryhard"],
    "deathpenalty": ["casual", "veryeasy", "easy", "hard", "hardcore"],
    "resources": ["muchless", "less", "more", "muchmore", "most"],
    "raids": ["none", "muchless", "less", "more", "muchmore"],
    "portals": ["casual", "hard", "veryhard"],
}
#: World presets accepted by ``-preset``.
PRESETS = ["normal", "casual", "easy", "hard", "hardcore", "immersive", "hammer"]


class ValidationError(ValueError):
    """Raised when an instance configuration would be rejected by the server."""


@dataclass(slots=True)
class InstanceConfig:
    """Everything needed to launch one dedicated server.

    Persisted as ``instance.json`` inside the instance directory, which makes
    each instance self-describing and portable.
    """

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = "My Valheim Server"
    world: str = "Dedicated"
    password: str = ""
    port: int = 2456
    #: Advertised in the Valheim server browser. Defaults on, because a server
    #: that is not listed looks broken from the outside -- reachable, joinable
    #: by address, and absent from the list -- with nothing to say why.
    #: Instances saved earlier keep whatever is in their instance.json.
    public: bool = True
    crossplay: bool = False
    preset: str = ""
    modifiers: dict[str, str] = field(default_factory=dict)
    save_interval: int = 1800
    backups: int = 4
    backup_short: int = 7200
    backup_long: int = 43200
    extra_args: str = ""
    mods_enabled: bool = False
    #: Minutes between automatic rollback snapshots; 0 turns them off. These
    #: are the manager's own snapshots, separate from Valheim's backups.
    snapshot_interval: int = 0
    #: How many automatic snapshots to keep before the oldest is pruned.
    snapshot_keep: int = 12
    #: Check this instance's mods against Thunderstore once a day. Off by
    #: default: which mods a server runs is the operator's decision, so the
    #: manager reports a release and does nothing else until told to.
    mod_update_check: bool = False
    #: Local time of day for that check, ``HH:MM``.
    mod_update_at: str = "05:00"
    #: Install what the check finds, rather than only reporting it.
    mod_update_install: bool = False
    #: Restart the server once an update has actually been installed, since a
    #: running server keeps loading the mods it started with.
    mod_update_restart: bool = False
    #: ``YYYY-MM-DD`` of the last scheduled check, so restarting the manager
    #: cannot make the day's check run twice.
    mod_update_last_run: str = ""
    #: This instance's Discord settings: which Discord server and channel, what
    #: it announces, who may ask about it. All of it per instance, so two
    #: servers can live in two unrelated Discords. Off by default, and off is
    #: silent: an instance that was never opted in does not start talking
    #: because another one did. Cleared when an instance is imported from an
    #: archive, since it names a Discord the importing operator cannot see.
    discord: InstanceDiscord = field(default_factory=InstanceDiscord)
    created_at: float = field(default_factory=time.time)

    # ------------------------------------------------------------------ #
    # serialisation
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "InstanceConfig":
        known = {f for f in cls.__dataclass_fields__}
        config = cls(**{k: v for k, v in payload.items() if k in known})
        # asdict() flattens the nested settings to a plain dict, so reading one
        # back has to rebuild it.
        config.discord = InstanceDiscord.from_dict(payload.get("discord"))
        # An instance.json written before the settings moved per-instance has
        # these two at the top level instead. The rest of what it needs -- the
        # guild, which events to announce -- was manager-wide, and is pushed
        # down from discord.json on the first start after the upgrade.
        if "discord" not in payload:
            config.discord.enabled = bool(payload.get("discord_enabled", False))
            config.discord.channel_id = str(payload.get("discord_channel_id") or "")
        return config

    # ------------------------------------------------------------------ #
    # derived values
    # ------------------------------------------------------------------ #
    @property
    def query_port(self) -> int:
        """Steam A2S query port. Valheim always listens on game port + 1."""
        return self.port + 1

    @property
    def slug(self) -> str:
        return slugify(self.name)

    def validate(self) -> None:
        """Reject configurations the server itself would refuse at boot."""
        if not self.name.strip():
            raise ValidationError("Server name cannot be empty.")
        if not self.world.strip():
            raise ValidationError("World name cannot be empty.")
        if not 1024 <= self.port <= 65530:
            raise ValidationError("Port must be between 1024 and 65530.")
        if self.password:
            if len(self.password) < 5:
                raise ValidationError("Password must be at least 5 characters.")
            # The server exits on boot if the password appears in either name.
            if self.password in self.name or self.password in self.world:
                raise ValidationError(
                    "Password cannot be contained in the server or world name."
                )
        elif self.public:
            raise ValidationError("A public server must have a password.")
        if self.preset and self.preset not in PRESETS:
            raise ValidationError(f"Unknown preset {self.preset!r}.")
        if self.snapshot_interval and not 5 <= self.snapshot_interval <= 10080:
            raise ValidationError(
                "Snapshot interval must be between 5 minutes and a week (or 0 to turn it off)."
            )
        if not 1 <= self.snapshot_keep <= 200:
            raise ValidationError("Keep between 1 and 200 automatic snapshots.")
        try:
            normalise_time_of_day(self.mod_update_at)
        except ValueError:
            raise ValidationError(
                "The mod update check needs a time of day like 05:00."
            ) from None
        try:
            self.discord.validate()
        except DiscordConfigError as exc:
            raise ValidationError(str(exc)) from None
        for key, value in self.modifiers.items():
            if key not in MODIFIER_KEYS:
                raise ValidationError(f"Unknown modifier {key!r}.")
            if value not in MODIFIER_KEYS[key]:
                raise ValidationError(f"Invalid value {value!r} for modifier {key!r}.")

    def launch_args(self, layout: "InstanceLayout") -> list[str]:
        """Build the dedicated server's command line."""
        args = [
            "-nographics",
            "-batchmode",
            "-name", self.name,
            "-port", str(self.port),
            "-world", self.world,
            "-savedir", str(layout.savedir),
            "-public", "1" if self.public else "0",
            "-saveinterval", str(self.save_interval),
            "-backups", str(self.backups),
            "-backupshort", str(self.backup_short),
            "-backuplong", str(self.backup_long),
        ]
        if self.password:
            args += ["-password", self.password]
        if self.crossplay:
            args.append("-crossplay")
        if self.preset:
            args += ["-preset", self.preset]
        for key, value in sorted(self.modifiers.items()):
            args += ["-modifier", key, value]
        if self.extra_args.strip():
            args += self.extra_args.split()
        return args


@dataclass(slots=True)
class InstanceLayout:
    """Filesystem layout for a single instance.

    The instance directory doubles as the BepInEx *profile* (r2modman calls it
    a profile): mods are installed into ``<root>/BepInEx`` and the server is
    launched with that directory as its working directory, so a modded and an
    unmodded instance differ only by what is on disk here.
    """

    root: Path

    @classmethod
    def for_instance(cls, settings: Settings, instance_id: str) -> "InstanceLayout":
        return cls(root=settings.instances_dir / instance_id)

    @property
    def config_file(self) -> Path:
        return self.root / "instance.json"

    @property
    def savedir(self) -> Path:
        return self.root / "saves"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def console_log(self) -> Path:
        return self.logs / "console.log"

    # -- BepInEx / mod profile ----------------------------------------- #
    @property
    def bepinex(self) -> Path:
        return self.root / "BepInEx"

    @property
    def plugins(self) -> Path:
        return self.bepinex / "plugins"

    @property
    def mods_manifest(self) -> Path:
        """r2modman keeps a ``mods.yml`` beside the profile; we use JSON."""
        return self.root / "mods.json"

    @property
    def doorstop_libs(self) -> Path:
        return self.root / "doorstop_libs"

    def ensure(self) -> None:
        for path in (self.root, self.savedir, self.logs):
            path.mkdir(parents=True, exist_ok=True)
