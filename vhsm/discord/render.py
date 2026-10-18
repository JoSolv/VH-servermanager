"""Turning manager state into Discord messages, to one design.

Everything the bot says goes through here, so that a server reads the same
whether you were told about it or asked about it. The rules, which the rest of
this package follows rather than decides for itself:

**One mark vocabulary.** Five glyphs, one meaning each, and nothing else:

===========  ======================================================
|UP|         up, or here: a server online, a player connected
|DOWN|       gone: a server shutting down, a player who left
|BUSY|       on its way: a server restarting
|MODS|       about this server's mods
|UPDATE|     about this server's version
===========  ======================================================

**Title is the subject, footer is the context.** An announcement is about the
server, so the server is in its title. A command reply is about whatever was
asked -- the mods, the players, one player -- so that is the title, and the
server (plus any count) goes in the footer. Nothing is named twice.

**No markdown in embed titles.** Discord renders them literally, so ``**x**``
would show the asterisks. Bold belongs in plain messages and in field values.

**One line is a message; a set of facts is an embed.** A join, a leave, a
server going down are one-line facts, and a channel of boxes for them reads
like an alarm going off rather than a server people play on.

**Bold the subject of the sentence.** "**Freyja** joined Midgard" and
"**Midgard** is shutting down" -- whoever or whatever the message is about,
once. Embeds get none, per the title rule above.

**Nothing is said twice.** The *online* announcement's title already says the
server is up, so its card leaves the status row out; ``/vh status`` has a
neutral title and needs it.

**Player-facing wording.** "running" and "stopped" are what a process does;
"Online" and "Offline" are what a player sees. :func:`status_label` is the one
place that translation happens.
"""

from __future__ import annotations

import time
from typing import Any

#: Discord's own palette, so embeds look native rather than approximate.
COLOURS: dict[str, int] = {
    "info": 0x5865F2,        # blurple
    "good": 0x57F287,        # green
    "important": 0xFEE75C,   # yellow
    "bad": 0xED4245,         # red
    "muted": 0x4F545C,
}

# The mark vocabulary. See the module docstring; nothing outside this list is
# used, so a glance down a channel is legible without reading any of it.
MARK_UP = "\U0001F7E2"        # green circle
MARK_BUSY = "\U0001F7E1"      # yellow circle
MARK_DOWN = "⚫"          # black circle
MARK_MODS = "\U0001F4E6"      # package
MARK_UPDATE = "⬆️"  # up arrow

#: Marks for a raw process status, for the operator-facing announcements that
#: still speak in those terms. Player-facing code uses :func:`status_label`.
STATUS_MARKS: dict[str, str] = {
    "running": MARK_UP,
    "starting": MARK_BUSY,
    "stopping": MARK_BUSY,
    "stopped": MARK_DOWN,
    "crashed": "\U0001F534",      # red circle: operator-only
}

STATUS_LEVELS: dict[str, str] = {
    "running": "good",
    "starting": "info",
    "stopping": "info",
    "stopped": "muted",
    "crashed": "bad",
}

#: Discord refuses an embed description over 4096 characters and a field value
#: over 1024. A long mod list is the realistic way to meet either.
DESCRIPTION_LIMIT = 4000
FIELD_LIMIT = 1000


def duration(seconds: float) -> str:
    """``9015`` -> ``2h 30m``. Short, and never zero-valued units."""
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m"


def timestamp(at: float, style: str = "R") -> str:
    """A Discord timestamp, rendered in each reader's own timezone.

    ``<t:1700000000:R>`` becomes "2 hours ago" for everyone looking, which is
    the right answer for a bot serving people in several places -- the
    manager's own local time is only right for whoever runs it.
    """
    if not at:
        return "never"
    return f"<t:{int(at)}:{style}>"


def truncate(text: str, limit: int = FIELD_LIMIT) -> str:
    """Keep *text* inside a Discord limit, saying that it was cut."""
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n... (truncated)"


def embed(
    title: str,
    *,
    description: str = "",
    level: str = "info",
    fields: list[dict[str, Any]] | None = None,
    footer: str = "",
) -> dict[str, Any]:
    """One embed, with the limits Discord enforces already applied.

    *title* is rendered literally by Discord, so it carries no markdown -- a
    mark and plain words only.
    """
    payload: dict[str, Any] = {
        "title": truncate(title, 250),
        "color": COLOURS.get(level, COLOURS["info"]),
    }
    if description:
        payload["description"] = truncate(description, DESCRIPTION_LIMIT)
    if fields:
        payload["fields"] = [
            {
                "name": truncate(str(f.get("name", "")), 250),
                "value": truncate(str(f.get("value", "") or "—")),
                "inline": bool(f.get("inline", True)),
            }
            # Discord allows 25 fields; nothing here comes close, but a
            # refused message is a worse outcome than a short one.
            for f in fields[:25]
        ]
    if footer:
        payload["footer"] = {"text": truncate(footer, 2000)}
    payload["timestamp"] = _iso(time.time())
    return payload


# --------------------------------------------------------------------------- #
# the server card, shared by the "online" announcement and /vh status
# --------------------------------------------------------------------------- #
def connectable(snapshot: dict[str, Any]) -> bool:
    """Whether a player could join right now.

    The kernel saying the process holds its game port, or the Steam query
    socket having answered. A running process that answers neither is still
    loading the world, which to a player is not "online".
    """
    return bool(snapshot.get("listening") or snapshot.get("query_answered"))


def status_label(snapshot: dict[str, Any]) -> tuple[str, str]:
    """What this server's state is called, and how to colour it.

    The one place process words become player words, so an announcement and a
    status reply never disagree about what "up" means.
    """
    status = str(snapshot.get("status") or "stopped")
    if status == "running":
        if connectable(snapshot):
            return f"{MARK_UP} Online", "good"
        # Up as far as the operating system is concerned, not yet as far as
        # anybody wanting to play is concerned.
        return f"{MARK_BUSY} Starting up", "important"
    if status in ("starting", "stopping"):
        wording = "Starting up" if status == "starting" else "Shutting down"
        return f"{MARK_BUSY} {wording}", "important"
    # A crash is an operator's problem; to a player the server is simply not
    # there, and saying "crashed" in their channel invites questions nobody
    # in it can answer.
    return f"{MARK_DOWN} Offline", "muted"


def count_of(number: int, noun: str) -> str:
    """``3, "mod"`` -> ``3 mods``. Player-facing text does not say "mod(s)"."""
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def server_fields(
    snapshot: dict[str, Any],
    address: str = "",
    *,
    show_address: bool = True,
    include_status: bool = True,
) -> list[dict[str, Any]]:
    """The facts about one server, as both ``/vh status`` and *online* show them.

    Deliberately short. Mod update counts, restart-pending warnings, exit codes
    and console notices are all things only the operator can act on, and they
    live on the instance page rather than in a player's channel.

    *include_status* is off for the *online* announcement, whose title has
    already said it.
    """
    players = snapshot.get("players") or {}
    mods = snapshot.get("mods") or {}

    fields: list[dict[str, Any]] = []
    if include_status:
        fields.append({"name": "Status", "value": status_label(snapshot)[0]})
    if connectable(snapshot):
        maximum = players.get("max") or 0
        count = players.get("count") or 0
        fields.append({
            "name": "Players",
            "value": f"{count}/{maximum}" if maximum else str(count),
        })
        if snapshot.get("uptime"):
            fields.append({"name": "Up for", "value": duration(snapshot["uptime"])})
    if snapshot.get("version"):
        fields.append({"name": "Version", "value": snapshot["version"]})
    if snapshot.get("mods_enabled"):
        fields.append({
            "name": "Mods",
            "value": f"{count_of(mods.get('enabled', 0), 'mod')} active",
        })
    if show_address and address:
        fields.append({"name": "Address", "value": f"`{address}`", "inline": False})
    return fields


def _iso(at: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))
