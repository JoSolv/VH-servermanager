"""Slash commands: what they are, who may run them, and what they answer.

Everything here is scoped by **channel**. One channel belongs to one instance,
so a command typed in it can only mean that server: there is no server
argument to pass, nothing to autocomplete, and no way to ask about anything
else. A channel with no instance behind it gets one sentence saying so.

That is a deliberately narrow surface. The people in a server's channel are
its players, and what they want is "is it up, who is on, what mods". They have
no business knowing that this host runs four other servers, what build the
shared game install is on, or that a manager exists at all -- so there is no
command that reports any of it. Nothing here reaches past the one instance the
channel names.

Two tiers, and the difference between them is the rest of the security design:

* **read** commands answer questions. The worst a stranger can do with one is
  learn how many people are on a server they can already see in the browser.
* **control** commands start and stop processes on the host. That is a hole
  through the reason the GUI binds to localhost, so the gate below treats an
  unconfigured control tier as "nobody", never as "anyone".

No control command is registered yet. The tier, the gate, the cooldown and the
audit trail are here and tested, so adding one is an entry in :data:`COMMANDS`
and a handler -- not a second look at the permission model.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..events import LIFECYCLE, Event
from . import render

log = logging.getLogger("vhsm.discord.commands")

# Interaction types Discord sends.
INTERACTION_PING = 1
INTERACTION_COMMAND = 2
INTERACTION_COMPONENT = 3

# Interaction response types.
RESPONSE_PONG = 1
RESPONSE_MESSAGE = 4
RESPONSE_DEFERRED = 5

#: Only the person who ran the command sees the reply.
FLAG_EPHEMERAL = 64

# Application command option types.
OPTION_SUBCOMMAND = 1
OPTION_STRING = 3

#: The one top-level command everything hangs off, so the bot occupies exactly
#: one name in a guild that may well have several bots in it.
ROOT = "vh"

class Denied(RuntimeError):
    """The command was refused, with a reason that is safe to show.

    Every refusal is phrased so it tells the user what to do about it without
    telling them anything about the host -- or about the other Discords this
    manager answers in -- that they could not already see.
    """


@dataclass(slots=True)
class Command:
    """One subcommand of ``/vh``."""

    name: str
    description: str
    #: ``read`` or ``control``. See the module docstring.
    tier: str
    handler: Callable[["CommandContext"], Awaitable[dict[str, Any]]]
    #: ``player`` commands are registered and answered. ``admin`` ones are
    #: kept, handler and all, and are neither offered nor answered until there
    #: is an admin channel to offer them in -- so a stale menu in somebody's
    #: client cannot reach one.
    audience: str = "player"
    #: Any arguments of its own. There is deliberately no "which server"
    #: argument on any command: the channel already says which, so offering
    #: one would be offering a way to ask about something else.
    extra_options: list[dict[str, Any]] = field(default_factory=list)
    #: Answer with "thinking..." first and fill it in after. For anything that
    #: may touch the network or take a moment: Discord allows three seconds
    #: for the first response and counts a late one as a failure.
    defer: bool = False

    def to_option(self) -> dict[str, Any]:
        options = list(self.extra_options)
        payload: dict[str, Any] = {
            "type": OPTION_SUBCOMMAND,
            "name": self.name,
            "description": self.description,
        }
        if options:
            payload["options"] = options
        return payload


@dataclass(slots=True)
class CommandContext:
    """Everything a handler needs, already narrowed to one server.

    *record* is the instance that owns the channel this command came from.
    There is exactly one, decided before any handler runs, so a handler can
    never reach past it.
    """

    manager: Any
    command: Command
    options: dict[str, Any]
    user_id: str
    user_name: str
    guild_id: str
    channel_id: str
    record: Any = None

    def actor(self) -> str:
        """How this caller is recorded in logs and announcements."""
        return f"discord:{self.user_name}" if self.user_name else f"discord:{self.user_id}"


# --------------------------------------------------------------------------- #
# scope: which instances an interaction can reach
# --------------------------------------------------------------------------- #
def interaction_user(interaction: dict[str, Any]) -> dict[str, Any]:
    """The calling user. A guild interaction carries ``member``, a DM ``user``."""
    member = interaction.get("member") or {}
    return member.get("user") or interaction.get("user") or {}


def _roles(interaction: dict[str, Any]) -> set[str]:
    member = interaction.get("member") or {}
    return {str(role) for role in member.get("roles") or ()}


def may_read(record: Any, interaction: dict[str, Any]) -> bool:
    """Whether this caller may ask about *record*, by its own settings."""
    settings = record.config.discord
    if not settings.answers_commands:
        return False
    if settings.channel_id != str(interaction.get("channel_id") or ""):
        return False
    # The channel already identifies the instance; the guild is checked too so
    # that a channel id reused in another Discord -- or a stale one left over
    # from a move -- cannot match.
    if settings.guild_id != str(interaction.get("guild_id") or ""):
        return False
    if settings.read_role_ids and not _roles(interaction) & set(settings.read_role_ids):
        return False
    return True


def instance_for_channel(manager: Any, interaction: dict[str, Any]) -> Any | None:
    """The one instance this interaction is about, or None.

    Channels are unique across instances (the manager refuses to save two
    with the same one), so this is a lookup rather than a search.
    """
    if not interaction.get("guild_id") or not interaction.get("channel_id"):
        return None
    for record in manager.records:
        if may_read(record, interaction):
            return record
    return None


def check_control(record: Any, interaction: dict[str, Any]) -> None:
    """Raise :class:`Denied` unless this caller may control *record*.

    Read access is a prerequisite -- a caller who cannot see a server cannot
    restart it -- and then the instance's own control settings decide.
    """
    settings = record.config.discord
    if not settings.control_enabled:
        raise Denied(
            f"Controlling **{record.config.name}** is switched off in the "
            "server manager."
        )
    # No "empty means anyone" here, unlike the read tier: an unconfigured
    # control allowlist means nobody, because the alternative is that
    # switching control on quietly hands the host to the whole channel.
    user_id = str(interaction_user(interaction).get("id") or "")
    allowed_user = user_id and user_id in settings.control_user_ids
    allowed_role = bool(_roles(interaction) & set(settings.control_role_ids))
    if not (allowed_user or allowed_role):
        raise Denied(f"You are not allowed to control **{record.config.name}**.")


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #
async def _status(ctx: CommandContext) -> dict[str, Any]:
    record = ctx.record
    snapshot = record.snapshot()
    status = str(snapshot["status"])
    players = snapshot.get("players") or {}
    mods = snapshot.get("mods") or {}

    fields: list[dict[str, Any]] = [
        {
            "name": "Status",
            "value": f"{render.STATUS_MARKS.get(status, '')} {status}"
            + (f" ({snapshot['operation']})" if snapshot.get("operation") else ""),
        },
        {"name": "World", "value": snapshot.get("world") or "—"},
    ]
    if status == "running":
        maximum = players.get("max") or 0
        count = players.get("count") or 0
        fields.append(
            {
                "name": "Players",
                "value": f"{count}/{maximum}" if maximum else str(count),
            }
        )
        if snapshot.get("uptime"):
            fields.append({"name": "Uptime", "value": render.duration(snapshot["uptime"])})
    if snapshot.get("version"):
        fields.append({"name": "Version", "value": snapshot["version"]})
    if record.config.discord.show_address:
        fields.append(
            {"name": "Address", "value": f"`{ctx.manager.address_for(record)}`", "inline": False}
        )
    if snapshot.get("mods_enabled"):
        value = f"{mods.get('enabled', 0)} active of {mods.get('installed', 0)} installed"
        if mods.get("updates_active"):
            value += f", {len(mods['updates_active'])} with an update"
        fields.append({"name": "Mods", "value": value, "inline": False})

    # The notices are the manager's own words for what is wrong, already
    # written for a person to read, so they are repeated rather than restated.
    description = "\n".join(
        f"⚠️ {notice['title']}" for notice in snapshot.get("notices") or []
    )
    if status == "crashed" and snapshot.get("exit_code") is not None:
        description = (
            f"Exited with code {snapshot['exit_code']}." + ("\n" + description if description else "")
        )

    return _embed_message(
        render.embed(
            snapshot["name"],
            description=description,
            level=render.STATUS_LEVELS.get(status, "info"),
            fields=fields,
        )
    )


async def _players(ctx: CommandContext) -> dict[str, Any]:
    record = ctx.record
    snapshot = record.snapshot()
    if not record.supervisor.status.is_active:
        return _message(f"**{record.config.name}** is {snapshot['status']}.")

    players = (snapshot.get("players") or {}).get("players") or []
    count = (snapshot.get("players") or {}).get("count") or 0
    if not players:
        # A count without names is the normal state for a few seconds after
        # someone joins, and on a server the query socket answers for before
        # the console has named anybody.
        if count:
            return _message(
                f"**{record.config.name}**: {count} player(s) connected, "
                "no names resolved yet."
            )
        return _message(f"Nobody is on **{record.config.name}**.")

    lines = []
    for player in players:
        bits = [f"• **{player['name']}**"]
        if player.get("in_world"):
            bits.append(f"· {render.duration(player.get('playtime') or 0)}")
        else:
            bits.append("· connecting")
        if player.get("platform") and player["platform"] != "unknown":
            bits.append(f"· {player['platform']}")
        lines.append(" ".join(bits))

    return _embed_message(
        render.embed(
            f"{record.config.name} — players",
            description="\n".join(lines),
            level="good",
            footer=f"{count} connected",
        )
    )


async def _mods(ctx: CommandContext) -> dict[str, Any]:
    """The mods this server is running: names and versions, and nothing else.

    What a player wants from this is the list to match in r2modman. Which of
    them has an update, and whether the server needs restarting to pick one
    up, are the operator's problem and are on the instance page.
    """
    record = ctx.record
    if not record.config.mods_enabled:
        return _message(f"**{record.config.name}** does not run mods.")

    profile = ctx.manager.profile(record.config.id)
    active = sorted(
        (mod for mod in profile.mods if mod.enabled),
        key=lambda mod: mod.name.lower(),
    )
    if not active:
        return _message(f"**{record.config.name}** has no mods enabled.")

    return _embed_message(
        render.embed(
            f"{record.config.name} \u2014 mods",
            description="\n".join(
                f"\u2022 {mod.name} `{mod.version}`" for mod in active
            ),
            level="info",
            footer=f"{len(active)} mod(s)",
        )
    )


async def _player(ctx: CommandContext) -> dict[str, Any]:
    """One player's stats, or everyone who has ever played here.

    With no name it is the roster, online first: the question "who plays on
    this server" and the question "is Freyja on right now" are the same
    question asked at two widths, so they are one command.

    Deliberately not restricted to who is online: the roster exists precisely
    so that somebody who left an hour ago can still be looked up. It is
    restricted to this one server, so asking in one channel cannot enumerate
    the players of another.
    """
    record = ctx.record
    online = {
        player.name.lower()
        for player in record.players.players
        if player.name
    }
    wanted = str(ctx.options.get("name") or "").strip()

    if not wanted:
        return _roster(record, online)

    entry = next(
        (e for e in record.roster.entries()
         if any(name.lower() == wanted.lower() for name in e.names)),
        None,
    )
    if entry is None:
        return _message(
            f"Nobody called **{wanted}** has played on **{record.config.name}**."
        )

    here = (entry.display_name or "").lower() in online or any(
        name.lower() in online for name in entry.names
    )
    fields = [
        {"name": "Sessions", "value": str(entry.sessions)},
        {"name": "Time played", "value": render.duration(entry.playtime)},
        {
            "name": "Last seen",
            "value": "**online now**" if here else render.timestamp(entry.last_seen),
            "inline": False,
        },
    ]
    if len(entry.names) > 1:
        fields.append({
            "name": "Also known as",
            "value": ", ".join(entry.names[:-1][-4:]),
            "inline": False,
        })

    return _embed_message(
        render.embed(
            entry.display_name or wanted,
            level="good" if here else "info",
            fields=fields,
            footer=record.config.name,
        )
    )


def _roster(record: Any, online: set[str]) -> dict[str, Any]:
    """Everyone who has played here, the ones online now first."""
    entries = record.roster.entries()
    if not entries:
        return _message(f"Nobody has played on **{record.config.name}** yet.")

    def is_here(entry: Any) -> bool:
        return any(name.lower() in online for name in entry.names)

    # Online first, then most recently seen -- which is what roster.entries()
    # already gives, so only the online ones need lifting.
    ordered = sorted(entries, key=lambda e: not is_here(e))
    lines = []
    for entry in ordered[:40]:
        name = entry.display_name or "(unnamed)"
        if is_here(entry):
            lines.append(f"\U0001F7E2 **{name}** \u00b7 online now")
        else:
            lines.append(
                f"\u26AB {name} \u00b7 {render.duration(entry.playtime)} played "
                f"\u00b7 {render.timestamp(entry.last_seen)}"
            )
    here = sum(1 for entry in entries if is_here(entry))
    footer = f"{len(entries)} player(s) ever, {here} online now"
    if len(ordered) > 40:
        footer += " \u00b7 showing the first 40"

    return _embed_message(
        render.embed(
            f"{record.config.name} \u2014 players",
            description="\n".join(lines),
            level="good" if here else "info",
            footer=footer,
        )
    )


# --------------------------------------------------------------------------- #
# the command table
# --------------------------------------------------------------------------- #
#: Every command, in the order Discord lists them.
#:
#: Every one is about the server whose channel it was typed in, and nothing
#: else. There is no command that reports the manager's own version, the shared
#: game install's build, or that any other server exists: the people in a
#: server's channel are its players, and none of that is theirs to see.
#:
#: ``audience="admin"`` keeps a command without offering or answering it, for
#: the second channel. Three are offered today -- status, player, mods -- which
#: is "is it up", "who plays here" and "what do I need to install".
#:
#: Adding a control command is one entry here plus its handler. The gate, the
#: cooldown and the audit trail already understand ``tier="control"``, and
#: :func:`definitions_for_guild` leaves control commands unregistered in a
#: guild where no instance has them switched on -- so they do not even appear
#: in Discord's menu until they can actually be used. A lifecycle handler
#: wants ``defer=True``, and should call ``ctx.manager.submit`` with
#: :meth:`CommandContext.actor` so the audit trail carries a name:
#:
#:     "restart": Command(
#:         name="restart", description="Restart this server", tier="control",
#:         handler=_restart, defer=True,
#:     ),
COMMANDS: dict[str, Command] = {
    "status": Command(
        name="status",
        description="Is this server up, who is on it, what version",
        tier="read",
        handler=_status,
    ),
    "player": Command(
        name="player",
        description="Everyone who plays here, or one player's stats",
        tier="read",
        handler=_player,
        extra_options=[
            {
                "type": OPTION_STRING,
                "name": "name",
                "description": "Character name. Leave it out to list everybody.",
                "required": False,
            }
        ],
    ),
    "mods": Command(
        name="mods",
        description="The mods this server is running",
        tier="read",
        handler=_mods,
    ),
    # -- kept for the admin channel ------------------------------------- #
    # Answered by nothing today: /vh player with no name is the player-facing
    # version of this, and a second command that lists only who is connected
    # is one more thing in the menu for no extra answer.
    "players": Command(
        name="players",
        description="Who is connected right now",
        tier="read",
        handler=_players,
        audience="admin",
    ),
}


def command_guilds(manager: Any) -> set[str]:
    """Every guild that has at least one instance answering commands."""
    return {
        record.config.discord.guild_id
        for record in manager.records
        if record.config.discord.answers_commands
    }


def definitions_for_guild(manager: Any, guild_id: str) -> list[dict[str, Any]]:
    """The command set to register in *guild_id*.

    Commands nobody in that guild could run are not registered there: a
    greyed-out or silently refusing command in the menu is a worse answer than
    one that is not offered. Control commands therefore appear only in a guild
    where some instance has control switched on -- which today is never, since
    no control command exists.
    """
    controllable = any(
        record.config.discord.control_enabled
        for record in manager.records
        if record.config.discord.answers_commands
        and record.config.discord.guild_id == guild_id
    )
    options = [
        command.to_option()
        for command in COMMANDS.values()
        if command.audience == "player"
        and (command.tier != "control" or controllable)
    ]
    return [
        {
            "name": ROOT,
            "description": "Valheim server manager",
            "type": 1,
            "options": options,
        }
    ]


class Cooldowns:
    """Per-user, per-instance rate limiting for control commands.

    A read is cheap and is not limited. A restart is not: without this, one
    person holding down a button is every player on the server disconnected
    repeatedly, and the manager's own "already restarting" guard would report
    that as an error rather than preventing it.
    """

    def __init__(self) -> None:
        self._last: dict[tuple[str, str, str], float] = {}

    def check(self, user_id: str, command: str, target: str, seconds: int) -> None:
        if seconds <= 0:
            return
        key = (user_id, command, target)
        now = time.monotonic()
        previous = self._last.get(key, 0.0)
        remaining = seconds - (now - previous)
        if previous and remaining > 0:
            raise Denied(f"Give that another {int(remaining) + 1}s.")
        self._last[key] = now


# --------------------------------------------------------------------------- #
# routing
# --------------------------------------------------------------------------- #
class CommandRouter:
    """Turns an interaction into a response payload.

    Holds no connection and does no I/O of its own beyond what a handler
    asks the manager for, which makes the whole command surface testable
    without a socket.
    """

    def __init__(self, manager: Any) -> None:
        self.manager = manager
        self.cooldowns = Cooldowns()

    # ------------------------------------------------------------------ #
    async def dispatch(self, interaction: dict[str, Any]) -> dict[str, Any] | None:
        """Handle one command interaction and return the response payload.

        Never raises: a refusal and a failure both come back as a message,
        because an interaction that goes unanswered shows the user "the
        application did not respond" and tells them nothing.
        """
        name, options, _ = parse_command(interaction)
        command = COMMANDS.get(name)
        # An admin command is stored, not available: a client holding a stale
        # menu must not be able to reach one, so this checks rather than
        # relying on it not having been registered.
        if command is None or command.audience != "player":
            return _reply(_message("I do not know that command.", ephemeral=True))

        if not interaction.get("guild_id"):
            return _reply(_message(
                "These commands only work in a server channel, not in a DM.",
                ephemeral=True,
            ))

        record = instance_for_channel(self.manager, interaction)
        if record is None:
            # Covers every reason at once -- no instance uses this channel,
            # the one that does has commands switched off, or this caller has
            # no permitted role -- and gives away nothing about the servers
            # that answer in other channels.
            return _reply(_message(
                "No Valheim server is set up for this channel.", ephemeral=True
            ))

        user = interaction_user(interaction)
        ctx = CommandContext(
            manager=self.manager,
            command=command,
            options=options,
            user_id=str(user.get("id") or ""),
            user_name=str(user.get("username") or ""),
            guild_id=str(interaction.get("guild_id") or ""),
            channel_id=str(interaction.get("channel_id") or ""),
            record=record,
        )

        try:
            if command.tier == "control":
                check_control(record, interaction)
                self.cooldowns.check(
                    ctx.user_id, command.name, record.config.id,
                    record.config.discord.cooldown_seconds,
                )
                # Recorded before the work is attempted, so a command that
                # takes the server down is in the log even if it then fails.
                self._audit(ctx, record)
            return _reply(await command.handler(ctx))
        except Denied as exc:
            return _reply(_message(str(exc), ephemeral=True))
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            log.exception("discord command /%s %s failed", ROOT, name)
            return _reply(_message(f"That failed: {exc}", ephemeral=True))

    def _audit(self, ctx: CommandContext, record: Any) -> None:
        """Record who asked for a control command, in the log and on the bus."""
        log.info(
            "discord: %s ran /%s %s on %s",
            ctx.actor(), ROOT, ctx.command.name, record.config.name,
        )
        events = getattr(ctx.manager, "events", None)
        if events is not None:
            events.publish(
                Event(
                    kind=LIFECYCLE,
                    instance_id=record.config.id,
                    instance_name=record.config.name,
                    title=f"{ctx.actor()} ran /{ROOT} {ctx.command.name} "
                          f"on {record.config.name}.",
                    level="info",
                    data={"actor": ctx.actor(), "command": ctx.command.name},
                )
            )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def parse_command(interaction: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
    """Pull the subcommand, its arguments and the focused one out.

    Returns ``(name, options, focused)``; *focused* is the argument the user
    is typing, which only an autocomplete interaction has.
    """
    data = interaction.get("data") or {}
    options = data.get("options") or []
    name, focused = "", ""
    arguments: dict[str, Any] = {}
    for option in options:
        if option.get("type") == OPTION_SUBCOMMAND:
            name = str(option.get("name") or "")
            for argument in option.get("options") or []:
                arguments[str(argument.get("name"))] = argument.get("value")
                if argument.get("focused"):
                    focused = str(argument.get("name"))
    return name, arguments, focused


def _message(content: str, *, ephemeral: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {"content": render.truncate(content, 1900)}
    if ephemeral:
        payload["flags"] = FLAG_EPHEMERAL
    return payload


def _embed_message(embed: dict[str, Any]) -> dict[str, Any]:
    return {"embeds": [embed]}


def _reply(data: dict[str, Any]) -> dict[str, Any]:
    return {"type": RESPONSE_MESSAGE, "data": data}

