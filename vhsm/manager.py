"""The manager: owns every instance, the sampling loop and scheduled updates."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable

from .config import Settings, settings as default_settings
from .instance import InstanceConfig, InstanceLayout, ValidationError
from .mods.profile import ModProfile
from .mods.thunderstore import ThunderstoreIndex
from .monitor.a2s import A2SError, query as a2s_query
from .monitor.metrics import ProcMetrics, ProcessSampler, host_metrics
from .monitor.net import NetworkMonitor, NetSample
from .monitor.players import PlayerTracker
from .playerlists import PlayerLists
from .steam import (
    SteamError,
    install_steamcmd,
    installed_build_id,
    latest_build_id,
    update_server,
    write_steam_appid,
)
from .supervisor import Supervisor
from .util import read_json, write_json

log = logging.getLogger("vhsm.manager")

#: Query the A2S socket every Nth sampling tick. Metrics are cheap, UDP
#: round-trips are not.
QUERY_EVERY_N_TICKS = 3
A2S_TIMEOUT = 1.5
#: Don't re-ask Steam for the newest build more often than this.
BUILD_CHECK_TTL = 900.0


class ManagerError(RuntimeError):
    pass


class JobLog:
    """Output of one long-running job (install, update), pollable by the UI."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.running = False
        self.title = ""

    def log(self, message: str) -> None:
        self.lines.append(message)
        del self.lines[:-400]

    async def run(self, title: str, body: Callable[[], Awaitable[None]]) -> bool:
        if self.running:
            self.log("[manager] another job is already running")
            return False
        self.running, self.title = True, title
        self.lines.clear()
        try:
            await body()
            return True
        except SteamError as exc:
            self.log(f"[error] {exc}")
        except asyncio.CancelledError:
            self.log("[error] cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            self.log(f"[error] unexpected: {exc}")
            log.exception("job %s failed", title)
        finally:
            self.running = False
            self.log("[manager] done")
        return False


@dataclass(slots=True)
class AutoUpdate:
    """Scheduled update settings, persisted in the data root."""

    enabled: bool = False
    #: Local time of day, ``HH:MM``.
    at: str = "04:00"
    restart_instances: bool = True
    validate: bool = False
    #: ``YYYY-MM-DD`` of the last run, so a restart cannot double-fire.
    last_run_date: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "at": self.at,
            "restart_instances": self.restart_instances,
            "validate": self.validate,
            "last_run_date": self.last_run_date,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AutoUpdate":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (payload or {}).items() if k in known})

    def due(self, now: datetime) -> bool:
        return (
            self.enabled
            and now.strftime("%H:%M") == self.at
            and self.last_run_date != now.strftime("%Y-%m-%d")
        )


@dataclass(slots=True)
class InstanceRecord:
    """Everything the manager keeps in memory about one instance."""

    config: InstanceConfig
    layout: InstanceLayout
    supervisor: Supervisor
    lists: PlayerLists
    players: PlayerTracker = field(default_factory=PlayerTracker)
    metrics: ProcMetrics = field(default_factory=ProcMetrics)
    net: NetSample = field(default_factory=NetSample)
    #: Version reported by the Steam query socket, when it answers.
    query_version: str = ""
    #: In-flight lifecycle operation, owned by the manager rather than by an
    #: HTTP request, so a browser navigating away cannot abandon it half-done.
    operation: asyncio.Task | None = None
    operation_name: str = ""

    @property
    def busy(self) -> bool:
        return self.operation is not None and not self.operation.done()

    @property
    def version(self) -> str:
        return self.supervisor.server_version or self.query_version

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
            "operation": self.operation_name if self.busy else "",
            "pid": supervisor.pid,
            "uptime": round(supervisor.uptime),
            "exit_code": supervisor.exit_code,
            "last_error": supervisor.last_error,
            "version": self.version,
            "players": self.players.to_dict(self.lists),
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
        self.job = JobLog()
        self._proc_sampler = ProcessSampler()
        self._records: dict[str, InstanceRecord] = {}
        self._sampler_task: asyncio.Task[None] | None = None
        self._scheduler_task: asyncio.Task[None] | None = None
        self._tick = 0

        self._state_file = self.settings.data_root / "manager.json"
        state = read_json(self._state_file, {}) or {}
        self.auto_update = AutoUpdate.from_dict(state.get("auto_update", {}))
        self._latest_build: str = str(state.get("latest_build", ""))
        self._latest_checked: float = float(state.get("latest_checked", 0) or 0)

    # ------------------------------------------------------------------ #
    # persisted manager state
    # ------------------------------------------------------------------ #
    def save_state(self) -> None:
        write_json(
            self._state_file,
            {
                "auto_update": self.auto_update.to_dict(),
                "latest_build": self._latest_build,
                "latest_checked": self._latest_checked,
            },
        )

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
        record = InstanceRecord(
            config=config,
            layout=layout,
            supervisor=supervisor,
            lists=PlayerLists(layout.savedir, layout.root),
        )
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
        await self._cancel_operation(record)
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
        record.query_version = ""
        self.net.watch(record.config.port)
        write_steam_appid(self.settings)
        await record.supervisor.start()

    async def stop(self, instance_id: str) -> None:
        record = self.get(instance_id)
        await record.supervisor.stop()
        record.players.reset()
        record.metrics = ProcMetrics()

    async def restart(self, instance_id: str) -> None:
        await self.stop(instance_id)
        await self.start(instance_id)

    def submit(self, instance_id: str, action: str) -> InstanceRecord:
        """Run a lifecycle action as a manager-owned background task.

        Stopping a real server takes as long as saving the world does, which
        is far longer than a browser will hold a request open. Tying the work
        to the request meant an aborted XHR cancelled the handler mid-way --
        a restart would stop the server and never start it again. The task is
        owned here instead, so the outcome no longer depends on the client.
        """
        record = self.get(instance_id)
        if record.busy:
            raise ManagerError(f"{record.config.name} is already {record.operation_name}.")

        operations = {"start": self.start, "stop": self.stop, "restart": self.restart}
        operation = operations.get(action)
        if operation is None:
            raise ManagerError(f"unknown action {action!r}")

        # Fail fast on problems we can see before committing to a background
        # task, so the button click reports them directly.
        if action in ("start", "restart"):
            record.config.validate()
            if not self.settings.server_binary.exists():
                raise ManagerError(
                    f"Server binary missing at {self.settings.server_binary}. "
                    "Install the dedicated server from Settings first."
                )
            if action == "start" and record.supervisor.status.is_active:
                raise ManagerError(f"{record.config.name} is already running.")

        async def run() -> None:
            try:
                await operation(instance_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - surfaced on the card
                record.supervisor.last_error = str(exc)
                log.warning("%s failed for %s: %s", action, record.config.name, exc)
            finally:
                record.operation_name = ""

        record.operation_name = action + "ing" if action != "stop" else "stopping"
        record.operation = asyncio.create_task(run())
        return record

    async def _cancel_operation(self, record: InstanceRecord) -> None:
        if record.operation and not record.operation.done():
            record.operation.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await record.operation
        record.operation_name = ""

    async def start_autostart(self) -> None:
        for record in self.records:
            if not record.config.autostart:
                continue
            try:
                await self.start(record.config.id)
            except (RuntimeError, ValidationError) as exc:
                log.warning("autostart failed for %s: %s", record.config.name, exc)

    async def shutdown_all(self) -> None:
        for record in self.records:
            await self._cancel_operation(record)
        active = [r for r in self.records if r.supervisor.status.is_active]
        if active:
            log.info("stopping %d running instance(s)", len(active))
        await asyncio.gather(
            *(r.supervisor.stop() for r in active), return_exceptions=True
        )

    # ------------------------------------------------------------------ #
    # updates
    # ------------------------------------------------------------------ #
    async def check_update(self, force: bool = False) -> dict[str, Any]:
        """Compare the installed build against the newest published one."""
        installed = installed_build_id(self.settings)
        stale = time.time() - self._latest_checked > BUILD_CHECK_TTL
        if (force or stale or not self._latest_build) and self.settings.steamcmd_bin.is_file():
            try:
                self._latest_build = await latest_build_id(self.settings)
                self._latest_checked = time.time()
                self.save_state()
            except SteamError as exc:
                log.warning("build check failed: %s", exc)

        return {
            "installed": installed,
            "latest": self._latest_build,
            "checked_at": self._latest_checked,
            # Only claim an update when both ids are known and differ.
            "available": bool(installed and self._latest_build and installed != self._latest_build),
        }

    async def run_update(self, *, validate: bool = False, restart: bool = True) -> None:
        """Install or update server files, restarting instances around it."""
        job = self.job

        async def body() -> None:
            running = [r for r in self.records if r.supervisor.status.is_active]
            if running and restart:
                job.log(f"[manager] stopping {len(running)} running instance(s)")
                for record in running:
                    job.log(f"[manager] stopping {record.config.name}")
                    await self.stop(record.config.id)
            elif running:
                raise SteamError(
                    "Instances are running and automatic restart is disabled. "
                    "Stop them first, or enable restarting."
                )

            await install_steamcmd(self.settings, job.log)
            job.log("[manager] running steamcmd app_update 896660")
            async for line in update_server(self.settings, validate=validate):
                job.log(line)
            write_steam_appid(self.settings)

            self._latest_build = installed_build_id(self.settings) or self._latest_build
            self._latest_checked = time.time()
            self.save_state()
            job.log(f"[manager] installed build {self._latest_build or 'unknown'}")

            if restart:
                for record in running:
                    job.log(f"[manager] starting {record.config.name}")
                    try:
                        await self.start(record.config.id)
                    except Exception as exc:  # noqa: BLE001
                        job.log(f"[error] could not restart {record.config.name}: {exc}")

        await job.run("update", body)

    async def _scheduler_loop(self) -> None:
        """Fire the scheduled update when its minute comes around."""
        while True:
            try:
                now = datetime.now()
                if self.auto_update.due(now) and not self.job.running:
                    log.info("scheduled update starting (%s)", self.auto_update.at)
                    self.auto_update.last_run_date = now.strftime("%Y-%m-%d")
                    self.save_state()
                    await self.run_update(
                        validate=self.auto_update.validate,
                        restart=self.auto_update.restart_instances,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduled update failed")
            # Re-check well inside the one-minute window the schedule matches.
            await asyncio.sleep(20)

    # ------------------------------------------------------------------ #
    # sampling
    # ------------------------------------------------------------------ #
    async def _query_instance(self, record: InstanceRecord) -> None:
        try:
            info = await a2s_query("127.0.0.1", record.config.query_port, A2S_TIMEOUT)
        except A2SError:
            # Normal while the world is still generating; keep log-derived state.
            return
        record.query_version = info.version
        record.players.observe_query(
            info.players, info.max_players, info.player_names, info.player_durations
        )

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

            # Lift kick bans whose time is up.
            try:
                for player_id in record.lists.reconcile_temp_bans():
                    log.info("kick ban expired for %s on %s", player_id, record.config.name)
            except OSError as exc:
                log.warning("could not reconcile bans for %s: %s", record.config.name, exc)

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

    def start_background(self) -> None:
        if self._sampler_task is None or self._sampler_task.done():
            self._sampler_task = asyncio.create_task(self._sampler_loop())
        if self._scheduler_task is None or self._scheduler_task.done():
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    async def stop_background(self) -> None:
        for attr in ("_sampler_task", "_scheduler_task"):
            task = getattr(self, attr)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                setattr(self, attr, None)
