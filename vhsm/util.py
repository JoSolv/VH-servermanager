"""Small helpers shared across the manager."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

_SLUG_RE = re.compile(r"[^a-z0-9]+")

#: How late a daily job may still run after the time it was set for. Both
#: schedules here -- the server update and each instance's mod check -- are
#: checked by a loop that can be busy, or stopped, at the appointed minute, and
#: skipping the whole day over a few seconds of drift is the worse failure. The
#: window is what keeps it from being the other kind of surprise: a manager
#: started in the evening catches up on nothing, it waits for tomorrow.
SCHEDULE_CATCH_UP_MINUTES = 60


def slugify(value: str) -> str:
    """Turn a display name into something safe to use as a directory name."""
    slug = _SLUG_RE.sub("-", value.strip().lower()).strip("-")
    return slug or "instance"


def parse_time_of_day(text: str) -> tuple[int, int]:
    """Read a ``HH:MM`` time of day. Raises ValueError on anything else."""
    hour, separator, minute = str(text or "").partition(":")
    if not separator:
        raise ValueError(f"{text!r} is not a time of day")
    hours, minutes = int(hour), int(minute)
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise ValueError(f"{text!r} is not a time of day")
    return hours, minutes


def normalise_time_of_day(text: str) -> str:
    """``4:5`` -> ``04:05``, so a stored schedule has one spelling."""
    hours, minutes = parse_time_of_day(text)
    return f"{hours:02d}:{minutes:02d}"


def daily_due(at: str, last_run_date: str, now: datetime) -> bool:
    """Whether a daily job set for *at* should run now.

    *last_run_date* is the ``YYYY-MM-DD`` the job last ran on, which is what
    keeps it to once a day however often this is asked -- including across a
    restart of the manager, as long as the caller persists it.
    """
    try:
        hours, minutes = parse_time_of_day(at)
    except ValueError:
        return False                  # an unreadable time never comes due
    if last_run_date == now.strftime("%Y-%m-%d"):
        return False
    late = (now.hour * 60 + now.minute) - (hours * 60 + minutes)
    return 0 <= late < SCHEDULE_CATCH_UP_MINUTES


def write_json(path: Path, payload: Any) -> None:
    """Write JSON atomically so a crash can never leave a truncated file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def human_bytes(value: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024.0:
            return f"{value:.1f} {unit}" if unit != "B" else f"{value:.0f} B"
        value /= 1024.0
    return f"{value:.1f} PiB"


def safe_relative(root: Path, target: Path) -> Path:
    """Resolve *target* and guarantee it stays inside *root*.

    Guards every path that is influenced by user input or by the contents of a
    downloaded archive, so a crafted name cannot escape the instance directory.
    """
    root = root.resolve()
    resolved = (root / target).resolve() if not target.is_absolute() else target.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"path {target!r} escapes {root}")
    return resolved
