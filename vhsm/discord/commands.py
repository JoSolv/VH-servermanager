"""Slash commands: what they are, who may run them, and what they answer.

Everything here is scoped by **guild**. An interaction arrives from one
Discord server, and the instances in scope are the ones whose own settings
name that guild -- so a server in somebody else's Discord is not merely
unlisted, it cannot be named, autocompleted or addressed from here at all.
That is what makes "a separate Discord per instance" mean anything: without
it, one leaked invite would expose every server this manager runs.

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
INTERACTION_AUTOCOMPLETE = 4

# Interaction response types.
RESPONSE_PONG = 1
RESPONSE_MESSAGE = 4
RESPONSE_DEFERRED = 5
RESPONSE_AUTOCOMPLETE = 8

#: Only the person who ran the command sees the reply.
FLAG_EPHEMERAL = 64

# Application command option types.
OPTION_SUBCOMMAND = 1
OPTION_STRING = 3

#: The one top-level command everything hangs off, so the bot occupies exactly
#: one name in a guild that may well have several bots in it.
ROOT = "vh"

#: Discord shows at most this many autocomplete choices.
AUTOCOMPLETE_LIMIT = 25


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
    #: Whether it takes the optional ``server`` argument, which autocompletes
    #: from the instances visible in this guild and may be left out when only
    #: one is.
    takes_server: bool = False
    extra_options: list[dict[str, Any]] = field(default_factory=list)
    #: Answer with "thinking..." first and fill it in after. For anything that
    #: may touch the network or take a moment: Discord allows three seconds
    #: for the first response and counts a late one as a failure.
    defer: bool = False

    def to_option(self) -> dict[str, Any]:
        options: list[dict[str, Any]] = []
        if self.takes_server:
            options.append(
                {
                    "type": OPTION_STRING,
                    "name": "server",
                    "description": "Which server (optional when only one is here)",
                    "required": False,
                    "autocomplete": True,
                }
            )
        options.extend(self.extra_options)
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
    """Everything a handler needs, already narrowed to this caller.

    *visible* is the whole of the caller's world: the instances in this guild
    whose settings let this person ask about them. Every handler works from it
    rather than from ``manager.records``, so there is one place that decides
    what an interaction can reach.
    """

    manager: Any
    command: Command
    options: dict[str, Any]
    user_id: str
    user_name: str
    guild_id: str
    channel_id: str
    visible: list[Any] = field(default_factory=list)

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
    if settings.guild_id != str(interaction.get("guild_id") or ""):
        return False
    channel_id = str(interaction.get("channel_id") or "")
    if settings.command_channel_ids and channel_id not in settings.command_channel_ids:
        return False
    if settings.read_role_ids and not _roles(interaction) & set(settings.read_role_ids):
        return False
    return True


def visible_instances(manager: Any, interaction: dict[str, Any]) -> list[Any]:
    """Every instance this interaction may reach, in display order."""
    if not interaction.get("guild_id"):
        return []
    return [record for record in manager.records if may_read(record, interaction)]


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
async def _servers(ctx: CommandContext) -> dict[str, Any]:
    lines = [render.status_line(record.snapshot()) for record in ctx.visible]
    return _embed_message(
        render.embed(
            "Servers",
            description="\n".join(lines),
            level="info",
            footer=f"{len(ctx.visible)} server(s)",
        )
    )


async def _status(ctx: CommandContext) -> dict[str, Any]:
    record = _resolve(ctx)
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
    record = _resolve(ctx)
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
    record = _resolve(ctx)
    if not record.config.mods_enabled:
        return _message(f"**{record.config.name}** does not run mods.")

    summary = ctx.manager.profile(record.config.id).summary()
    active = [mod for mod in summary["mods"] if mod["enabled"]]
    if not active:
        return _message(f"**{record.config.name}** has no mods enabled.")

    lines = [f"• {mod['name']} `{mod['version']}`" for mod in active]
    fields = []
    updates = summary.get("updates_active") or []
    if updates:
        fields.append(
            {
                "name": f"{len(updates)} update(s) available",
                "value": "\n".join(
                    f"{u['name']} {u['current']} → {u['latest']}" for u in updates
                ),
                "inline": False,
            }
        )
    if record.restart_pending:
        fields.append(
            {
                "name": "Restart pending",
                "value": "Mods have changed since this server started, so it is "
                         "not running them yet.",
                "inline": False,
            }
        )

    return _embed_message(
        render.embed(
            f"{record.config.name} — mods",
            description="\n".join(lines),
            level="important" if updates or record.restart_pending else "info",
            fields=fields,
            footer=f"{len(active)} active of {summary['count']} installed",
        )
    )


async def _player(ctx: CommandContext) -> dict[str, Any]:
    """Roster stats for one player, across the servers visible here.

    Deliberately not restricted to who is online: the roster exists precisely
    so that somebody who left an hour ago can still be looked up. It *is*
    restricted to this guild's servers, so asking in one Discord cannot
    enumerate the players of a server that belongs to another.
    """
    wanted = str(ctx.options.get("name") or "").strip().lower()
    if not wanted:
        raise Denied("Which player? Give a character name.")

    online_now: set[str] = set()
    found: list[tuple[Any, Any]] = []
    for record in ctx.visible:
        for player in record.players.players:
            if player.name and player.name.lower() == wanted:
                online_now.add(record.config.id)
        for entry in record.roster.entries():
            if any(name.lower() == wanted for name in entry.names):
                found.append((record, entry))

    if not found:
        return _message(
            f"Nobody called **{ctx.options.get('name')}** has joined "
            "any of these servers."
        )

    fields = []
    for record, entry in found[:10]:
        here = record.config.id in online_now
        value = (
            f"Seen {entry.sessions} time(s), {render.duration(entry.playtime)} played\n"
            f"Last seen: {'**online now**' if here else render.timestamp(entry.last_seen)}"
        )
        if len(entry.names) > 1:
            value += f"\nAlso known as: {', '.join(entry.names[:-1][-4:])}"
        fields.append({"name": record.config.name, "value": value, "inline": False})

    newest = max(entry.last_seen for _, entry in found)
    return _embed_message(
        render.embed(
            found[0][1].display_name or str(ctx.options.get("name")),
            level="good" if online_now else "info",
            fields=fields,
            footer="online now" if online_now else f"last seen {_ago(newest)}",
        )
    )


async def _version(ctx: CommandContext) -> dict[str, Any]:
    """Versions: the manager's own, Steam's newest, and this guild's servers."""
    from ..config import build_info

    build = await ctx.manager.check_update()
    manager_build = build_info()

    fields = [
        {
            "name": "Manager",
            "value": f"vhsm {manager_build['version']}"
            + (f" (`{manager_build['short_commit']}`)" if manager_build["short_commit"] else ""),
            "inline": False,
        },
        {
            "name": "Dedicated server files",
            "value": (
                f"build `{build.get('installed') or 'unknown'}`"
                + (
                    f" — newest is `{build['latest']}`"
                    if build.get("latest") and build.get("available")
                    else " — up to date"
                    if build.get("latest")
                    else ""
                )
            ),
            "inline": False,
        },
    ]
    running = [
        f"{record.config.name}: {record.version}"
        for record in ctx.visible
        if record.supervisor.status.is_active and record.version
    ]
    if running:
        fields.append({"name": "Running", "value": "\n".join(running), "inline": False})

    return _embed_message(
        render.embed(
            "Versions",
            level="important" if build.get("available") else "info",
            fields=fields,
        )
    )


# --------------------------------------------------------------------------- #
# the command table
# --------------------------------------------------------------------------- #
#: Every command, in the order Discord lists them.
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
#:         name="restart", description="Restart a server", tier="control",
#:         handler=_restart, takes_server=True, defer=True,
#:     ),
COMMANDS: dict[str, Command] = {
    "servers": Command(
        name="servers",
        description="List the servers in this Discord and what they are doing",
        tier="read",
        handler=_servers,
    ),
    "status": Command(
        name="status",
        description="Status, players, version and address for one server",
        tier="read",
        handler=_status,
        takes_server=True,
    ),
    "players": Command(
        name="players",
        description="Who is on a server right now",
        tier="read",
        handler=_players,
        takes_server=True,
    ),
    "mods": Command(
        name="mods",
        description="Which mods a server is running, and what has an update",
        tier="read",
        handler=_mods,
        takes_server=True,
    ),
    "player": Command(
        name="player",
        description="Stats for one player, online or not",
        tier="read",
        handler=_player,
        extra_options=[
            {
                "type": OPTION_STRING,
                "name": "name",
                "description": "Character name",
                "required": True,
            }
        ],
    ),
    "version": Command(
        name="version",
        description="Manager, server and Steam build versions",
        tier="read",
        handler=_version,
        # Asks Steam for the newest build, which is cached but may be cold.
        defer=True,
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
        if command.tier != "control" or controllable
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
    def autocomplete(self, interaction: dict[str, Any]) -> dict[str, Any]:
        """Offer the names of servers visible in this guild, and no others."""
        _, options, focused = parse_command(interaction)
        typed = str(options.get(focused) or "").lower()
        choices = [
            {"name": record.config.name[:100], "value": record.config.name[:100]}
            for record in visible_instances(self.manager, interaction)
            if typed in record.config.name.lower()
        ]
        return {
            "type": RESPONSE_AUTOCOMPLETE,
            "data": {"choices": choices[:AUTOCOMPLETE_LIMIT]},
        }

    async def dispatch(self, interaction: dict[str, Any]) -> dict[str, Any] | None:
        """Handle one command interaction and return the response payload.

        Never raises: a refusal and a failure both come back as a message,
        because an interaction that goes unanswered shows the user "the
        application did not respond" and tells them nothing.
        """
        name, options, _ = parse_command(interaction)
        command = COMMANDS.get(name)
        if command is None:
            return _reply(_message("I do not know that command.", ephemeral=True))

        if not interaction.get("guild_id"):
            return _reply(_message(
                "These commands only work in a server channel, not in a DM.",
                ephemeral=True,
            ))

        visible = visible_instances(self.manager, interaction)
        if not visible:
            # Covers every reason at once -- no instance belongs to this
            # guild, none has commands on, this channel is not allowed, or
            # this caller has no permitted role -- and gives away nothing
            # about servers that answer elsewhere.
            return _reply(_message(
                "No servers here are set up to answer you.", ephemeral=True
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
            visible=visible,
        )

        try:
            if command.tier == "control":
                record = _resolve(ctx)
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


def _resolve(ctx: CommandContext) -> Any:
    """Which instance the command is about, out of those visible here.

    The argument may be left out when only one server is visible, which is the
    common case and saves typing a name the bot already knows. A server that
    exists but belongs to another guild is not findable: as far as this
    interaction is concerned it does not exist, and the refusal says the same
    thing it would say for a typo.
    """
    records = ctx.visible
    wanted = str(ctx.options.get("server") or "").strip()
    if not wanted:
        if len(records) == 1:
            return records[0]
        raise Denied(
            "Which server? " + ", ".join(f"**{r.config.name}**" for r in records[:10])
        )

    lowered = wanted.lower()
    for record in records:
        if record.config.name.lower() == lowered or record.config.id == wanted:
            return record
    partial = [r for r in records if lowered in r.config.name.lower()]
    if len(partial) == 1:
        return partial[0]
    if partial:
        raise Denied(
            f"{wanted!r} matches several servers: "
            + ", ".join(f"**{r.config.name}**" for r in partial[:10])
        )
    raise Denied(
        f"No server called {wanted!r} here. "
        + ", ".join(f"**{r.config.name}**" for r in records[:10])
    )


def _message(content: str, *, ephemeral: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {"content": render.truncate(content, 1900)}
    if ephemeral:
        payload["flags"] = FLAG_EPHEMERAL
    return payload


def _embed_message(embed: dict[str, Any]) -> dict[str, Any]:
    return {"embeds": [embed]}


def _reply(data: dict[str, Any]) -> dict[str, Any]:
    return {"type": RESPONSE_MESSAGE, "data": data}


def _ago(at: float) -> str:
    if not at:
        return "never"
    return render.duration(time.time() - at) + " ago"
