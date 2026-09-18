"""Shared Jinja environment.

Kept out of ``app.py`` so route modules can import it without creating a cycle
back through the application factory.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates

from ..config import build_info
from ..util import human_bytes

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
TEMPLATES = Jinja2Templates(directory=str(TEMPLATE_DIR))


def duration(seconds: float) -> str:
    """Render a number of seconds as a compact uptime string."""
    seconds = int(seconds or 0)
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def timeago(timestamp: float) -> str:
    """Render a past timestamp as an approximate age."""
    if not timestamp:
        return "unknown"
    seconds = max(0, time.time() - float(timestamp))
    if seconds < 90:
        return "just now"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.0f} min ago"
    hours = minutes / 60
    if hours < 24:
        return f"{hours:.0f}h ago"
    days = hours / 24
    if days < 30:
        return f"{days:.0f}d ago"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")


def as_datetime(timestamp: float) -> str:
    if not timestamp:
        return "never"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


#: Modifier keys read badly as-is in a one-line summary.
MODIFIER_LABELS = {"deathpenalty": "death penalty"}


def rules_note(config: Any) -> str:
    """One line describing the world rules, for the Configuration summary.

    The port and world name already sit in the page header, so the collapsed
    section is more use saying what the world plays like.
    """
    bits: list[str] = []
    if getattr(config, "preset", ""):
        bits.append(f"{config.preset} preset")
    for key, value in (getattr(config, "modifiers", None) or {}).items():
        bits.append(f"{value} {MODIFIER_LABELS.get(key, key)}")
    return " \u00b7 ".join(bits) if bits else "default world rules"


def rate(bytes_per_second: float) -> str:
    return f"{human_bytes(bytes_per_second or 0)}/s"


TEMPLATES.env.filters["human_bytes"] = human_bytes
TEMPLATES.env.filters["duration"] = duration
TEMPLATES.env.filters["rate"] = rate
TEMPLATES.env.globals["vhsm_build"] = build_info()
TEMPLATES.env.filters["timeago"] = timeago
TEMPLATES.env.filters["datetime"] = as_datetime
TEMPLATES.env.filters["rules_note"] = rules_note
