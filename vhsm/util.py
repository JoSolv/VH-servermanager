"""Small helpers shared across the manager."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    """Turn a display name into something safe to use as a directory name."""
    slug = _SLUG_RE.sub("-", value.strip().lower()).strip("-")
    return slug or "instance"


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
