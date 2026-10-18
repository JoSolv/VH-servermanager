"""What the operator configured, and where the token lives.

Kept apart from :class:`~vhsm.config.Settings` because this is state an
operator edits in the GUI, like ``manager.json``, rather than deployment
configuration. The token is kept apart from *both*: see :func:`read_token`.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from ..events import DEFAULT_ANNOUNCE, KINDS
from ..util import read_json, write_json

#: Discord ids ("snowflakes") are decimal integers, currently 17-20 digits.
#: The range is deliberately loose at both ends -- early ids are shorter and
#: the format will keep growing -- but the shape is not: these are pasted in
#: by hand and then interpolated into API paths, so anything that is not a
#: run of digits is refused rather than sent.
_SNOWFLAKE = re.compile(r"^\d{5,22}$")

#: Permissions the invite link asks for: View Channel, Send Messages and
#: Embed Links. Nothing else -- the bot reads no messages and manages nothing.
INVITE_PERMISSIONS = 1 << 10 | 1 << 11 | 1 << 14


class DiscordConfigError(ValueError):
    """A configuration value the API would reject, or that is not an id."""


def normalise_id(value: str, label: str = "id") -> str:
    """Check one snowflake. Blank passes through; anything odd is refused."""
    text = str(value or "").strip()
    if not text:
        return ""
    if not _SNOWFLAKE.match(text):
        raise DiscordConfigError(
            f"{label} must be a Discord id -- digits only. Turn on Developer "
            "Mode in Discord, then right-click and Copy ID."
        )
    return text


def normalise_ids(values: Any, label: str = "id") -> list[str]:
    """Read a list of snowflakes from a list or a separated string.

    The GUI submits one textarea, so commas, spaces and newlines all separate.
    Order is kept and duplicates are dropped, which makes the saved list read
    back the way it was typed.
    """
    if isinstance(values, str):
        values = re.split(r"[\s,;]+", values)
    seen: list[str] = []
    for raw in values or ():
        one = normalise_id(raw, label)
        if one and one not in seen:
            seen.append(one)
    return seen


@dataclass(slots=True)
class DiscordConfig:
    """Everything the operator chose, persisted as ``discord.json``.

    The token is not here, on purpose.
    """

    enabled: bool = False
    #: The one guild this bot answers in. Commands from anywhere else are
    #: ignored: a token that leaks, or an invite into a second guild, must not
    #: become a way to reach this host.
    guild_id: str = ""
    #: Where events that belong to no single instance go -- server updates,
    #: and the manager starting. Also the fallback for an instance that has
    #: announcements on but no channel of its own.
    default_channel_id: str = ""

    # -- announcements --------------------------------------------------- #
    #: Per-event-kind switches, keyed by the constants in :mod:`vhsm.events`.
    announce: dict[str, bool] = field(
        default_factory=lambda: dict(DEFAULT_ANNOUNCE)
    )
    #: Whether a status announcement may carry the join address. On a public
    #: server it is already in the server browser; on a private one, posting
    #: it to a channel is a decision rather than a detail.
    show_address: bool = True

    # -- commands --------------------------------------------------------- #
    commands_enabled: bool = True
    #: Channels where commands are answered. Empty means every channel the
    #: bot can see in the configured guild.
    command_channel_ids: list[str] = field(default_factory=list)
    #: Roles allowed to run read-only commands. Empty means anyone who can
    #: already see an allowed channel, which is Discord's own gate.
    read_role_ids: list[str] = field(default_factory=list)

    # -- control (start/stop/restart) -------------------------------------- #
    #: Off, and worth leaving off. A control command lets anyone who can get
    #: into the channel start and stop processes on this host, which is the
    #: thing the manager binding to localhost exists to prevent.
    control_enabled: bool = False
    #: Roles allowed to run control commands. There is no "empty means
    #: anyone" here: empty means nobody, and the gate says so.
    control_role_ids: list[str] = field(default_factory=list)
    #: Individual users allowed to, for an operator who wants no role for it.
    control_user_ids: list[str] = field(default_factory=list)
    #: Per-user, per-instance cooldown, so a control command cannot be held
    #: down. Applies to control commands only; a read is cheap.
    cooldown_seconds: int = 15

    # ------------------------------------------------------------------ #
    def announces(self, kind: str) -> bool:
        """Whether *kind* is announced. Unknown kinds fall back to the default."""
        if kind in self.announce:
            return bool(self.announce[kind])
        return bool(DEFAULT_ANNOUNCE.get(kind, False))

    @property
    def configured(self) -> bool:
        """Whether there is enough here to connect and say anything."""
        return bool(self.guild_id)

    def validate(self) -> None:
        self.guild_id = normalise_id(self.guild_id, "Server (guild) ID")
        self.default_channel_id = normalise_id(
            self.default_channel_id, "Default channel ID"
        )
        self.command_channel_ids = normalise_ids(
            self.command_channel_ids, "Command channel ID"
        )
        self.read_role_ids = normalise_ids(self.read_role_ids, "Read role ID")
        self.control_role_ids = normalise_ids(self.control_role_ids, "Control role ID")
        self.control_user_ids = normalise_ids(self.control_user_ids, "Control user ID")
        if not 0 <= self.cooldown_seconds <= 3600:
            raise DiscordConfigError("Cooldown must be between 0 seconds and an hour.")
        if self.enabled and not self.guild_id:
            raise DiscordConfigError(
                "The bot needs the Discord server (guild) ID it should answer in."
            )
        # Keep only known kinds, so a renamed event cannot leave a dead switch
        # on disk that silently matches nothing.
        self.announce = {
            kind: bool(self.announce.get(kind, DEFAULT_ANNOUNCE.get(kind, False)))
            for kind in KINDS
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DiscordConfig":
        known = {f for f in cls.__dataclass_fields__}
        config = cls(**{k: v for k, v in (payload or {}).items() if k in known})
        # A config file written by a newer version, or edited by hand, is not
        # a reason to refuse to start: normalise what we can and drop the rest.
        config.announce = {
            kind: bool(config.announce.get(kind, DEFAULT_ANNOUNCE.get(kind, False)))
            for kind in KINDS
        }
        return config


def config_path(data_root: Path) -> Path:
    return data_root / "discord.json"


def load(data_root: Path) -> DiscordConfig:
    return DiscordConfig.from_dict(read_json(config_path(data_root), {}) or {})


def save(data_root: Path, config: DiscordConfig) -> None:
    write_json(config_path(data_root), config.to_dict())


# --------------------------------------------------------------------------- #
# the token
# --------------------------------------------------------------------------- #
def token_path(data_root: Path) -> Path:
    return data_root / "discord_token"


def read_token(data_root: Path) -> str:
    """The bot token, from the environment or from its own file.

    The environment wins, so a container can keep the token in a Docker
    secret or a compose ``environment:`` and never write it to the data
    volume at all.

    It lives in a file of its own rather than in ``discord.json`` for two
    reasons: the file can be ``0600`` while the config stays readable, and
    nothing that renders or exports configuration can reach it by accident.
    Unlike the server passwords -- which are in ``instance.json`` because the
    server needs them on its command line -- a bot token is an identity, and
    it is never rendered back into the GUI.
    """
    from_env = os.environ.get("VHSM_DISCORD_TOKEN", "").strip()
    if from_env:
        return from_env
    try:
        return token_path(data_root).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def token_source(data_root: Path) -> str:
    """Where the token came from, for the GUI: ``env`` | ``file`` | ``""``."""
    if os.environ.get("VHSM_DISCORD_TOKEN", "").strip():
        return "env"
    return "file" if read_token(data_root) else ""


def write_token(data_root: Path, token: str) -> None:
    """Store the token, readable only by this user.

    Created with ``0600`` from the start rather than chmod-ed afterwards, so
    there is no window in which it is world-readable.
    """
    cleaned = str(token or "").strip()
    path = token_path(data_root)
    if not cleaned:
        path.unlink(missing_ok=True)
        return
    # A pasted token with whitespace or quotes around it is the common
    # accident, and the resulting 401 says nothing useful.
    cleaned = cleaned.strip("\"'").strip()
    if cleaned.lower().startswith("bot "):
        cleaned = cleaned[4:].strip()
    if "\n" in cleaned or " " in cleaned:
        raise DiscordConfigError("That does not look like a bot token.")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(cleaned + "\n")


def clear_token(data_root: Path) -> None:
    token_path(data_root).unlink(missing_ok=True)


def invite_url(application_id: str) -> str:
    """Where the operator clicks to put the bot in their server."""
    return (
        "https://discord.com/oauth2/authorize"
        f"?client_id={application_id}"
        f"&permissions={INVITE_PERMISSIONS}"
        "&scope=bot%20applications.commands"
    )
