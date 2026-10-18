"""What the operator configured, and where the token lives.

The split here is the whole shape of the integration:

:class:`InstanceDiscord`
    One instance's settings -- which Discord server and channel, what it
    announces, who may ask about it. Persisted inside that instance's
    ``instance.json``, so it travels with the instance and so two servers can
    live in two entirely different Discords with nothing in common.

:class:`DiscordConfig`
    What is left over once everything that can be per-instance is: whether the
    bot runs at all. Persisted as ``discord.json``.

The token is in neither: see :func:`read_token`.

This module deliberately imports nothing from the rest of this package, so
:mod:`vhsm.instance` can read :class:`InstanceDiscord` without pulling in
``httpx`` or ``websockets``.
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


# --------------------------------------------------------------------------- #
# per-instance
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class InstanceDiscord:
    """One instance's Discord settings, stored in its ``instance.json``.

    Everything that could sensibly differ between two servers is here rather
    than on the manager, because the thing people actually have is one Discord
    per group of players: a public server posting to a community guild, and a
    modded test server in a guild of three friends, sharing nothing but the
    bot account.
    """

    #: Master switch for this instance. Off means it is neither announced nor
    #: answerable, whatever else is set here.
    enabled: bool = False
    #: **The channel is this instance's identity in Discord.** Announcements go
    #: here, and a command typed here can only mean this server -- which is
    #: why the commands take no server argument and why no two instances may
    #: share a channel. Required whenever this instance uses Discord at all.
    channel_id: str = ""
    #: Which Discord server that channel is in. Not typed in by hand: it is
    #: looked up from the channel and stored, because command registration
    #: needs a guild id before any interaction has arrived. Blank means the
    #: lookup has not succeeded yet -- usually the bot has not been invited to
    #: that Discord.
    guild_id: str = ""

    # -- announcements --------------------------------------------------- #
    #: Per-event-kind switches, keyed by the constants in :mod:`vhsm.events`.
    announce: dict[str, bool] = field(
        default_factory=lambda: dict(DEFAULT_ANNOUNCE)
    )
    #: Whether this instance's announcements and status replies may carry its
    #: join address. On a public server it is already in the server browser;
    #: on a private one, posting it to a channel is a decision rather than a
    #: detail -- and the answer can differ per server, which is why it is here.
    show_address: bool = True

    # -- commands --------------------------------------------------------- #
    #: Whether commands answer about this instance at all.
    commands_enabled: bool = True
    #: Roles allowed to ask about this instance. Empty means anyone who can
    #: see its channel, which is Discord's own gate and usually the right one.
    read_role_ids: list[str] = field(default_factory=list)

    # -- control (start/stop/restart) -------------------------------------- #
    #: Off, and worth leaving off. A control command lets anyone who can get
    #: into the channel start and stop processes on this host, which is the
    #: thing the manager binding to localhost exists to prevent. Per instance,
    #: so a test server can be restartable by its three users while the public
    #: one is not restartable by anybody.
    control_enabled: bool = False
    #: Roles allowed to control this instance. There is no "empty means
    #: anyone" here: empty means nobody, and the gate says so.
    control_role_ids: list[str] = field(default_factory=list)
    #: Individual users allowed to, for an operator who wants no role for it.
    control_user_ids: list[str] = field(default_factory=list)
    #: Per-user cooldown on control commands for this instance, so one cannot
    #: be held down. Reads are cheap and are not limited.
    cooldown_seconds: int = 15

    # ------------------------------------------------------------------ #
    def announces(self, kind: str) -> bool:
        """Whether *kind* is announced for this instance."""
        if not self.enabled:
            return False
        if kind in self.announce:
            return bool(self.announce[kind])
        return bool(DEFAULT_ANNOUNCE.get(kind, False))

    @property
    def announces_anything(self) -> bool:
        return self.enabled and any(self.announce.get(kind) for kind in KINDS)

    @property
    def answers_commands(self) -> bool:
        """Whether a command typed in this instance's channel is answered.

        Needs the channel, since that is how the interaction is matched, and
        the guild, since that is where the command had to be registered.
        """
        return (
            self.enabled
            and self.commands_enabled
            and bool(self.channel_id)
            and bool(self.guild_id)
        )

    def validate(self) -> None:
        """Reject what Discord would, and what would silently do nothing."""
        self.guild_id = normalise_id(self.guild_id, "Discord server (guild) ID")
        self.channel_id = normalise_id(self.channel_id, "Channel ID")
        self.read_role_ids = normalise_ids(self.read_role_ids, "Read role ID")
        self.control_role_ids = normalise_ids(self.control_role_ids, "Control role ID")
        self.control_user_ids = normalise_ids(self.control_user_ids, "Control user ID")
        if not 0 <= self.cooldown_seconds <= 3600:
            raise DiscordConfigError("Cooldown must be between 0 seconds and an hour.")
        self.announce = {
            kind: bool(self.announce.get(kind, DEFAULT_ANNOUNCE.get(kind, False)))
            for kind in KINDS
        }
        # The channel is the identity, not just a destination: it is where
        # announcements go *and* how a command is matched to this server. So
        # it is required outright rather than only when announcing.
        if self.enabled and not self.channel_id:
            raise DiscordConfigError(
                "This server needs the ID of its Discord channel. That channel "
                "is what identifies it: announcements go there, and a command "
                "typed there is about this server."
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Any) -> "InstanceDiscord":
        if isinstance(payload, cls):
            return payload
        known = {f for f in cls.__dataclass_fields__}
        config = cls(**{
            k: v for k, v in (payload or {}).items() if k in known
        }) if isinstance(payload, dict) else cls()
        config.announce = {
            kind: bool(config.announce.get(kind, DEFAULT_ANNOUNCE.get(kind, False)))
            for kind in KINDS
        }
        return config


# --------------------------------------------------------------------------- #
# manager-wide
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class DiscordConfig:
    """What is left once everything per-instance has moved out.

    Only the master switch. Everything else about *what* is said, *where*, and
    *by whom* belongs to an instance, and the token belongs to neither.
    """

    enabled: bool = False
    #: Whether the one-time move of the old manager-wide settings down onto
    #: the instances has happened. See :func:`legacy_settings`.
    migrated: bool = False

    def validate(self) -> None:
        return None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DiscordConfig":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (payload or {}).items() if k in known})


def config_path(data_root: Path) -> Path:
    return data_root / "discord.json"


def load(data_root: Path) -> DiscordConfig:
    return DiscordConfig.from_dict(read_json(config_path(data_root), {}) or {})


def save(data_root: Path, config: DiscordConfig) -> None:
    write_json(config_path(data_root), config.to_dict())


def legacy_settings(data_root: Path) -> dict[str, Any]:
    """The pre-per-instance settings still sitting in ``discord.json``.

    Read from the raw file rather than from :class:`DiscordConfig`, which no
    longer has fields for them. Returned so the bot can push them down onto
    the instances once, on first start after the upgrade: an operator who had
    the bot working should not have to set it all up again.
    """
    payload = read_json(config_path(data_root), {}) or {}
    return {
        "guild_id": str(payload.get("guild_id") or ""),
        "channel_id": str(payload.get("default_channel_id") or ""),
        "announce": payload.get("announce") or {},
        "show_address": bool(payload.get("show_address", True)),
        "commands_enabled": bool(payload.get("commands_enabled", True)),
        "read_role_ids": payload.get("read_role_ids") or [],
        "control_enabled": bool(payload.get("control_enabled", False)),
        "control_role_ids": payload.get("control_role_ids") or [],
        "control_user_ids": payload.get("control_user_ids") or [],
        "cooldown_seconds": int(payload.get("cooldown_seconds") or 15),
    }


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

    There is one token however many Discord servers are involved. A bot
    account can be invited to any number of guilds, so "a separate Discord per
    instance" needs separate *guild ids*, which are per-instance, not separate
    bots.
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
    """Where the operator clicks to put the bot in a server.

    The same link is used for every Discord server the bot should join: one
    bot account, invited as many times as there are guilds.
    """
    return (
        "https://discord.com/oauth2/authorize"
        f"?client_id={application_id}"
        f"&permissions={INVITE_PERMISSIONS}"
        "&scope=bot%20applications.commands"
    )
