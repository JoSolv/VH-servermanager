"""The manager: owns every instance and the background sampling loop."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
from dataclasses import dataclass, field
from typing import Any

from .config import Settings, settings as default_settings
from .instance import InstanceConfig, InstanceLayout, ValidationError
from .mods.profile import ModProfile
from .mods.thunderstore import ThunderstoreIndex
from .monitor.a2s import A2SError, query as a2s_query
from .monitor.metrics import ProcMetrics, ProcessSampler, host_metrics
from .monitor.net import NetworkMonitor, NetSample
from .monitor.players import PlayerTracker
from .supervisor import Status, Supervisor
from .util import read_json

log = logging.getLogger("vhsm.manager")

#: Query the A2S socket every Nth sampling tick. Metrics are cheap, UDP
#: round-trips are not.
QUERY_EVERY_N_TICKS = 3
A2S_TIMEOUT = 1.5


class ManagerError(RuntimeError):
    pass


@dataclass(slots=True)
class InstanceRecord:
    """Everything the manager keeps in memory about one instance."""

    config: InstanceConfig
    layout: InstanceLayout
    supervisor: Supervisor
    players: PlayerTracker = field(default_factory=PlayerTracker)
    metrics: ProcMetrics = field(default_factory=ProcMetrics)
    net: NetSample = field(default_factory=NetSample)

    def snapshot(self) -> dict[str, Any]:
        config, supervisor = self.config, self.supervisor
        return {
            "id": config.id,
            "name": config.name,
            "world": config.world,
            "port": config.port,
            "query_port": config.query_port,
            "public": config.public,
            "crossplay": config.crossplay,
            "mods_enabled": config.mods_enabled,
            "autostart": config.autostart,
            "status": supervisor.status.value,
            "pid": supervisor.pid,
            "uptime": round(supervisor.uptime),
            "exit_code": supervisor.exit_code,
            "last_error": supervisor.last_error,
            "players": self.players.to_dict(),
            "metrics": self.metrics.to_dict(),
            "net": self.net.to_dict(),
        }


class Hub:
    """Fan-out of snapshot broadcasts to connected browsers."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()

    def subscribe(self) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=32)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        self._subscribers.discard(queue)

    @property
    def listeners(self) -> int:
        return len(self._subscribers)

    def publish(self, payload: dict[str, Any]) -> None:
        if not self._subscribers:
            return
        message = json.dumps(payload, default=str)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # A browser that cannot keep up simply misses a frame.
                pass


class InstanceManager:
    """Registry and orchestration for every configured server."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or default_settings
        self.settings.ensure_dirs()
        self.index = ThunderstoreIndex(self.settings.cache_dir, self.settings.index_ttl)
        self.hub = Hub()
        self.net = NetworkMonitor()
        self._proc_sampler = ProcessSampler()
        self._records: dict[str, InstanceRecord] = {}
        self._sampler_task: asyncio.Task[None] | None = None
        self._tick = 0

    # ------------------------------------------------------------------ #
    # registry
    # ------------------------------------------------------------------ #
    def load_all(self) -> None:
        """Discover instances by scanning the data directory.

        The directory is the source of truth, so an instance can be moved or
        restored simply by copying its folder.
        """
        self._records.clear()
        if not self.settings.instances_dir.is_dir():
            return
        for child in sorted(self.settings.instances_dir.iterdir()):
            if not child.is_dir():
                continue
            payload = read_json(child / "instance.json")
            if not isinstance(payload, dict):
                log.warning("skipping %s: no readable instance.json", child)
                continue
            try:
                config = InstanceConfig.from_dict(payload)
            except TypeError as exc:
                log.warning("skipping %s: %s", child, exc)
                continue
            self._register(config)
        log.info("loaded %d instance(s)", len(self._records))

    def _register(self, config: InstanceConfig) -> InstanceRecord:
        layout = InstanceLayout.for_instance(self.settings, config.id)
        supervisor = Supervisor(config, layout, self.settings)
        record = InstanceRecord(config=config, layout=layout, supervisor=supervisor)
        supervisor.add_log_hook(record.players.observe_log)
        self._records[config.id] = record
        self.net.watch(config.port)
        return record

    @property
    def records(self) -> list[InstanceRecord]:
        return sorted(self._records.values(), key=lambda r: r.config.name.lower())

    def get(self, instance_id: str) -> InstanceRecord:
        record = self._records.get(instance_id)
        if record is None:
            raise ManagerError(f"no instance with id {instance_id!r}")
        return record

    def profile(self, instance_id: str) -> ModProfile:
        record = self.get(instance_id)
        return ModProfile(record.layout, self.settings.cache_dir, self.index)

    # ------------------------------------------------------------------ #
    # CRUD
    # ------------------------------------------------------------------ #
    def _assert_port_free(self, port: int, *, exclude: str = "") -> None:
        # Valheim binds port, port+1 and port+2, so ranges must not overlap.
        for record in self._records.values():
            if record.config.id == exclude:
                continue
            if abs(record.config.port - port) < 3:
                raise ValidationError(
                    f"Port {port} conflicts with {record.config.name!r} "
                    f"(uses {record.config.port}-{record.config.port + 2})."
                )

    def create(self, config: InstanceConfig) -> InstanceRecord:
        config.validate()
        self._assert_port_free(config.port)
        if any(r.config.name.lower() == config.name.lower() for r in self._records.values()):
            raise ValidationError(f"An instance named {config.name!r} already exists.")

        layout = InstanceLayout.for_instance(self.settings, config.id)
        layout.ensure()
        record = self._register(config)
        self.save(config.id)
        log.info("created instance %s (%s)", config.name, config.id)
        return record

    def save(self, instance_id: str) -> None:
        record = self.get(instance_id)
        from .util import write_json

        write_json(record.layout.config_file, record.config.to_dict())

    def update(self, instance_id: str, changes: dict[str, Any]) -> InstanceRecord:
        record = self.get(instance_id)
        merged = record.config.to_dict()
        merged.update(changes)
        merged["id"] = record.config.id          # identity is immutable
        merged["created_at"] = record.config.created_at

        updated = InstanceConfig.from_dict(merged)
        updated.validate()
        self._assert_port_free(updated.port, exclude=instance_id)

        if record.supervisor.status.is_active and updated.port != record.config.port:
            raise ValidationError("Stop the server before changing its port.")

        if updated.port != record.config.port:
            self.net.unwatch(record.config.port)
            self.net.watch(updated.port)

        record.config = updated
        record.supervisor.config = updated
        self.save(instance_id)
        return record

    async def delete(self, instance_id: str, *, remove_files: bool = False) -> None:
        record = self.get(instance_id)
        if record.supervisor.status.is_active:
            await record.supervisor.stop()
        self.net.unwatch(record.config.port)
        if record.supervisor.pid:
            self._proc_sampler.forget(record.supervisor.pid)
        self._records.pop(instance_id, None)
        if remove_files:
            shutil.rmtree(record.layout.root, ignore_errors=True)
        log.info("deleted instance %s (files removed: %s)", instance_id, remove_files)

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def start(self, instance_id: str) -> None:
        record = self.get(instance_id)
        record.players.reset()
        self.net.watch(record.config.port)
        await record.supervisor.start()

    async def stop(self, instance_id: str) -> None:
        record = self.get(instance_id)
        await record.supervisor.stop()
        record.players.reset()
        record.metrics = ProcMetrics()

    async def restart(self, instance_id: str) -> None:
        await self.stop(instance_id)
        await self.start(instance_id)

    async def start_autostart(self) -> None:
        for record in self.records:
            if not record.config.autostart:
                continue
            try:
                await self.start(record.config.id)
            except (RuntimeError, ValidationError) as exc:
                log.warning("autostart failed for %s: %s", record.config.name, exc)

    async def shutdown_all(self) -> None:
        active = [r for r in self.records if r.supervisor.status.is_active]
        if active:
            log.info("stopping %d running instance(s)", len(active))
        await asyncio.gather(
            *(r.supervisor.stop() for r in active), return_exceptions=True
        )

    # ------------------------------------------------------------------ #
    # sampling
    # ------------------------------------------------------------------ #
    async def _query_instance(self, record: InstanceRecord) -> None:
        try:
            info = await a2s_query("127.0.0.1", record.config.query_port, A2S_TIMEOUT)
        except A2SError:
            # Normal while the world is still generating; keep log-derived state.
            return
        record.players.observe_query(info.players, info.max_players, info.player_names)

    async def sample_once(self) -> dict[str, Any]:
        """Take one sample across every instance and return the broadcast payload."""
        self._tick += 1
        self.net.refresh()

        running = [r for r in self.records if r.supervisor.status.is_active]
        if running and self._tick % QUERY_EVERY_N_TICKS == 0:
            await asyncio.gather(
                *(self._query_instance(r) for r in running), return_exceptions=True
            )

        for record in self.records:
            if record.supervisor.status.is_active:
                record.metrics = self._proc_sampler.sample(
                    record.supervisor.pid, record.supervisor.started_at
                )
                record.net = self.net.for_port(record.config.port)
            else:
                record.metrics = ProcMetrics()
                record.net = NetSample(source=record.net.source)
                if record.players.count:
                    record.players.reset()

        return {
            "type": "snapshot",
            "host": host_metrics(),
            "host_net": self.net.host().to_dict(),
            "net_per_instance": {
                "available": self.net.per_instance_available,
                "reason": self.net.per_instance_reason,
            },
            "instances": [record.snapshot() for record in self.records],
        }

    async def _sampler_loop(self) -> None:
        interval = self.settings.sample_interval
        while True:
            try:
                payload = await self.sample_once()
                self.hub.publish(payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("sampler tick failed")
            await asyncio.sleep(interval)

    def start_sampler(self) -> None:
        if self._sampler_task is None or self._sampler_task.done():
            self._sampler_task = asyncio.create_task(self._sampler_loop())

    async def stop_sampler(self) -> None:
        if self._sampler_task is not None:
            self._sampler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sampler_task
            self._sampler_task = None
