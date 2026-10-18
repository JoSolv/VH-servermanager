"""Turning manager state into Discord messages.

Shared by commands and announcements so one server reads the same either way:
the same status wording, the same colours, the same way of saying "two hours".
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

#: A glance at the channel should answer "is it up" before anything is read.
STATUS_MARKS: dict[str, str] = {
    "running": "\U0001F7E2",     # green circle
    "starting": "\U0001F7E1",    # yellow circle
    "stopping": "\U0001F7E1",
    "stopped": "⚫",         # black circle
    "crashed": "\U0001F534",     # red circle
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
    """One embed, with the limits Discord enforces already applied."""
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


def status_line(snapshot: dict[str, Any]) -> str:
    """One line for an instance, as ``/vh servers`` lists them."""
    status = str(snapshot.get("status") or "stopped")
    mark = STATUS_MARKS.get(status, "⚫")
    name = snapshot.get("name") or "?"
    bits: list[str] = [f"{mark} **{name}** — {status}"]
    if snapshot.get("operation"):
        bits.append(f"({snapshot['operation']})")
    players = snapshot.get("players") or {}
    if status == "running":
        count = players.get("count") or 0
        maximum = players.get("max") or 0
        bits.append(f"· {count}/{maximum} players" if maximum else f"· {count} players")
        if snapshot.get("uptime"):
            bits.append(f"· up {duration(snapshot['uptime'])}")
    if snapshot.get("version"):
        bits.append(f"· v{snapshot['version']}")
    return " ".join(bits)


def _iso(at: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))
