"""Per-process resource metrics via psutil."""

from __future__ import annotations

import time
from dataclasses import dataclass, asdict
from typing import Any

import psutil


@dataclass(slots=True)
class ProcMetrics:
    """A single resource sample for one instance's process tree."""

    cpu_percent: float = 0.0
    memory_rss: int = 0
    memory_percent: float = 0.0
    threads: int = 0
    open_files: int = 0
    disk_read: int = 0
    disk_write: int = 0
    uptime: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProcessSampler:
    """Samples CPU/memory for a pid and its children.

    ``psutil`` reports CPU as the share used since the *previous* call on the
    same object, so the handle is cached per pid; recreating it every tick
    would report 0.0 forever.
    """

    def __init__(self) -> None:
        self._handles: dict[int, psutil.Process] = {}

    def _handle(self, pid: int) -> psutil.Process:
        handle = self._handles.get(pid)
        if handle is None or not handle.is_running():
            handle = psutil.Process(pid)
            handle.cpu_percent(None)  # prime the baseline
            self._handles[pid] = handle
        return handle

    def forget(self, pid: int) -> None:
        self._handles.pop(pid, None)

    def sample(self, pid: int | None, started_at: float | None = None) -> ProcMetrics:
        if pid is None:
            return ProcMetrics()
        try:
            proc = self._handle(pid)
            with proc.oneshot():
                metrics = ProcMetrics(
                    cpu_percent=proc.cpu_percent(None),
                    memory_rss=proc.memory_info().rss,
                    memory_percent=proc.memory_percent(),
                    threads=proc.num_threads(),
                    uptime=time.time() - (started_at or proc.create_time()),
                )
            try:
                metrics.open_files = len(proc.open_files())
            except (psutil.AccessDenied, OSError):
                pass
            try:
                io = proc.io_counters()
                metrics.disk_read, metrics.disk_write = io.read_bytes, io.write_bytes
            except (psutil.AccessDenied, AttributeError):
                pass

            # Roll up children; the real server does not fork, but a wrapper
            # script or the fake server might.
            for child in proc.children(recursive=True):
                try:
                    with child.oneshot():
                        metrics.cpu_percent += child.cpu_percent(None)
                        metrics.memory_rss += child.memory_info().rss
                        metrics.threads += child.num_threads()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            return metrics
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
            self.forget(pid)
            return ProcMetrics()


def host_metrics() -> dict[str, Any]:
    """Whole-machine snapshot shown in the dashboard header."""
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    load = psutil.getloadavg() if hasattr(psutil, "getloadavg") else (0.0, 0.0, 0.0)
    return {
        "cpu_percent": psutil.cpu_percent(None),
        "cpu_count": psutil.cpu_count(logical=True) or 1,
        "memory_total": memory.total,
        "memory_used": memory.used,
        "memory_percent": memory.percent,
        "disk_total": disk.total,
        "disk_used": disk.used,
        "disk_percent": disk.percent,
        "load": [round(value, 2) for value in load],
    }
