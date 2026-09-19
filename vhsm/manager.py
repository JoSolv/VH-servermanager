"""The manager: owns every instance, the sampling loop and scheduled updates."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import archive as archive_mod
from . import backups as backups_mod
from . import worlds as worlds_mod
from .config import Settings, settings as default_settings
from .diagnostics import check_libraries
from .instance import InstanceConfig, InstanceLayout, ValidationError
from .mods.profile import ModProfile
from .mods.thunderstore import ThunderstoreIndex
from .monitor.a2s import A2SError, query as a2s_query
from .monitor.metrics import ProcMetrics, ProcessSampler, host_metrics
from .monitor.net import NetworkMonitor, NetSample
from .monitor.ports import Endpoint, bound_udp_sockets, primary_host_ip, query_candidates
from .monitor.players import PlayerTracker
from .playerlists import PlayerLists
from .roster import Roster
from .steam import (
    SteamError,
    install_steamcmd,
    installed_build_id,
    latest_build_id,
    update_server,
    write_steam_appid,
)
from .supervisor import Supervisor
from .util import read_json, slugify, write_json

log = logging.getLogger("vhsm.manager")

#: Query the A2S socket every Nth sampling tick. Metrics are cheap, UDP
#: round-trips are not.
QUERY_EVERY_N_TICKS = 3
A2S_TIMEOUT = 1.5
#: Shorter, because discovery tries several addresses in a row.
DISCOVER_TIMEOUT = 0.8
#: How often to probe each instance from its public address.
REACHABILITY_EVERY_N_TICKS = 15
#: Don't re-ask Steam for the newest build more often than this.
BUILD_CHECK_TTL = 900.0


class ManagerError(RuntimeError):
    pass


class JobLog:
    """Output of one long-running job (install, update), pollable by the UI.

    Also appended to a file, so the full steamcmd transcript survives the
    in-memory tail being trimmed and can be downloaded after the fact -- which
    is exactly when a failed install needs reading.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.lines: list[str] = []
        self.running = False
        self.title = ""
        self.path = path

    def log(self, message: str) -> None:
        self.lines.append(message)
        del self.lines[:-400]
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as sink:
                sink.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
        except OSError:
            pass                      # a log that cannot be written is not fatal

    async def run(self, title: str, body: Callable[[], Awaitable[None]]) -> bool:
        if self.running:
            self.log("[manager] another job is already running")
            return False
        self.running, self.title = True, title
        self.lines.clear()
        self.log(f"=== {title} started ===")
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
    roster: Roster
    players: PlayerTracker = field(default_factory=PlayerTracker)
    metrics: ProcMetrics = field(default_factory=ProcMetrics)
    net: NetSample = field(default_factory=NetSample)
    #: Version reported by the Steam query socket, when it answers.
    query_version: str = ""
    #: The query socket we found for this process, once one answers. Cached so
    #: the candidate scan does not repeat on every tick.
    query_endpoint: Endpoint | None = None
    query_failures: int = 0
    #: In-flight lifecycle operation, owned by the manager rather than by an
    #: HTTP request, so a browser navigating away cannot abandon it half-done.
    operation: asyncio.Task | None = None
    operation_name: str = ""
    #: Result of the last external reachability probe. Tri-state: True when
    #: the Steam query socket answered, False when it did not, None when not
    #: applicable (server stopped, or crossplay, which has no such socket).
    reachable: bool | None = None
    #: Whether the process holds its game port. This comes from the kernel
    #: rather than from a network round trip, so it is a fact rather than an
    #: inference, and it is what "the server is up" actually means.
    listening: bool = False
    reachable_at: float = 0.0
    reachable_detail: str = ""
    #: When the last automatic snapshot was taken.
    last_auto_snapshot: float = 0.0

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
            "status": supervisor.status.value,
            "operation": self.operation_name if self.busy else "",
            "pid": supervisor.pid,
            "uptime": round(supervisor.uptime),
            "exit_code": supervisor.exit_code,
            "last_error": supervisor.last_error,
            # Known failures recognised in this run. Carried on every tick so
            # an open page learns about one without being reloaded -- which
            # matters, because these are raised while a server is running.
            "notices": supervisor.notices,
            "version": self.version,
            "reachable": self.reachable,
            "listening": self.listening,
            "reachable_at": self.reachable_at,
            "reachable_detail": self.reachable_detail,
            "players": self.players.to_dict(self.lists),
            "metrics": self.metrics.to_dict(),
            "net": self.net.to_dict(),
        }


@dataclass(slots=True)
class WorldImport:
    """What importing a world actually did, so the UI can say it plainly."""

    world: worlds_mod.World
    #: The name the upload carried, which differs from ``world.name`` when it
    #: was installed over an existing world under that world's name.
    source_name: str = ""
    #: The rollback point taken of the world that was replaced, if any.
    snapshot: backups_mod.Restore | None = None
    #: True when an existing world was overwritten rather than filled in.
    replaced: bool = False
    #: Set when the instance was repointed at the uploaded world's name.
    renamed_from: str = ""


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
        self.job = JobLog(self.settings.data_root / "steamcmd.log")
        self._proc_sampler = ProcessSampler()
        self._records: dict[str, InstanceRecord] = {}
        self._sampler_task: asyncio.Task[None] | None = None
        self._scheduler_task: asyncio.Task[None] | None = None
        self._tick = 0

        self._state_file = self.settings.data_root / "manager.json"
        state = read_json(self._state_file, {}) or {}
        self.auto_update = AutoUpdate.from_dict(state.get("auto_update", {}))
        #: Hostname or IP players connect to, used for the address shown in the
        #: UI and for the reachability probe. Blank falls back to this host's
        #: own address, which is right on a LAN and wrong behind NAT.
        self.public_hostname: str = str(state.get("public_hostname", "") or "")
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
                "public_hostname": self.public_hostname,
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
        roster = Roster(layout.root / "players.json")
        record = InstanceRecord(
            config=config,
            layout=layout,
            supervisor=supervisor,
            lists=PlayerLists(layout.savedir, layout.root),
            roster=roster,
        )
        # The tracker is the only place a character name can be paired with the
        # id that connected, so the roster is fed from its resolved events
        # rather than by parsing the log a second time.
        record.players = PlayerTracker(on_event=roster.observe)
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
        record.query_endpoint = None
        record.query_failures = 0
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

    async def shutdown_all(self) -> None:
        for record in self.records:
            await self._cancel_operation(record)
        active = [r for r in self.records if r.supervisor.status.is_active]
        if active:
            log.info("stopping %d running instance(s)", len(active))
        await asyncio.gather(
            *(r.supervisor.stop() for r in active), return_exceptions=True
        )
        for record in self.records:
            record.roster.save()

    # ------------------------------------------------------------------ #
    # players
    # ------------------------------------------------------------------ #
    def player_rows(self, instance_id: str) -> dict[str, Any]:
        """The roster plus live and moderation state, for the players panel."""
        record = self.get(instance_id)
        online = {
            p.player_id for p in record.players.players if p.player_id
        }
        return {
            "players": record.roster.summary(record.lists, online),
            "lists": record.lists.summary(),
            "online_count": len(online),
        }

    def set_permitted_enabled(self, instance_id: str, enabled: bool) -> bool:
        """Switch the whitelist on or off without discarding who is on it.

        Off is the default and only an operator turns it on: see
        :class:`~vhsm.playerlists.PlayerList`.
        """
        record = self.get(instance_id)
        changed = record.lists.permitted.set_enabled(enabled)
        record.roster.save(force=True)
        return changed

    def forget_player(self, instance_id: str, player_id: str) -> bool:
        return self.get(instance_id).roster.forget(player_id)

    def player_history(self, instance_id: str, player_id: str) -> tuple[str, str]:
        """A player's recorded events as downloadable text."""
        record = self.get(instance_id)
        entry = record.roster.get(player_id)
        if entry is None:
            raise ManagerError(f"no record of player {player_id!r}")
        header = [
            f"# Player history - {record.config.name}",
            f"# id: {entry.player_id}",
            f"# names: {', '.join(entry.names) or 'unknown'}",
            f"# sessions: {entry.sessions}",
            f"# first seen: {datetime.fromtimestamp(entry.first_seen):%Y-%m-%d %H:%M:%S}",
            f"# last seen: {datetime.fromtimestamp(entry.last_seen):%Y-%m-%d %H:%M:%S}",
            "",
        ]
        body = [
            f"{datetime.fromtimestamp(e['at']):%Y-%m-%d %H:%M:%S}  {e['label']}"
            for e in entry.events
        ]
        name = entry.display_name or entry.player_id
        return "\n".join(header + body) + "\n", f"{name}-history.txt"

    # ------------------------------------------------------------------ #
    # addressing and reachability
    # ------------------------------------------------------------------ #
    @property
    def hostname(self) -> str:
        """What players type to connect. Falls back to this host's address."""
        return self.public_hostname.strip() or primary_host_ip()

    def address_for(self, record: InstanceRecord) -> str:
        return f"{self.hostname}:{record.config.port}"

    async def check_reachable(self, record: InstanceRecord) -> bool | None:
        """Probe the instance the way a player's client would.

        Queries the public address rather than loopback, so it exercises the
        path players actually take. A failure here is not proof the server is
        unreachable from the internet: many routers do not loop a request back
        to themselves (NAT hairpinning), so a probe from the server's own host
        can fail while outside clients connect fine. The UI says so.
        """
        record.reachable_at = time.time()
        if not record.supervisor.status.is_active:
            record.reachable = None
            record.listening = False
            record.reachable_detail = "server is not running"
            return None

        # Ask the kernel first. Whether the process holds its game port is a
        # fact; whether a query answers is a round trip that can fail for
        # reasons that have nothing to do with the server being up.
        sockets = bound_udp_sockets(record.supervisor.pid)
        record.listening = any(e.port == record.config.port for e in sockets)

        if record.config.crossplay:
            # A crossplay server talks to players through a PlayFab relay and
            # is joined by code, so there is no Steam query socket to answer
            # and no port to forward. Calling that "unreachable" is wrong.
            record.reachable = None
            record.reachable_detail = (
                "crossplay is on, so players join by code through the PlayFab "
                "relay and there is no Steam query port to probe"
            )
            return None

        port = (
            record.query_endpoint.port
            if record.query_endpoint is not None
            else record.config.query_port
        )
        try:
            await a2s_query(self.hostname, port, A2S_TIMEOUT)
        except A2SError as exc:
            record.reachable = False
            record.reachable_detail = (
                f"{exc}. The process {'is' if record.listening else 'is not'} holding "
                f"UDP {record.config.port}."
            )
        else:
            record.reachable = True
            record.reachable_detail = f"answered on {self.hostname}:{port}"
        return record.reachable

    # ------------------------------------------------------------------ #
    # export / import
    # ------------------------------------------------------------------ #
    def _unique_name(self, name: str) -> str:
        existing = {r.config.name.lower() for r in self._records.values()}
        if name.lower() not in existing:
            return name
        for index in range(2, 100):
            candidate = f"{name} ({index})"
            if candidate.lower() not in existing:
                return candidate
        return f"{name} {uuid.uuid4().hex[:6]}"

    def _free_port(self, preferred: int) -> int:
        """The preferred port, or the next free 3-port range above it.

        A clone or an import wants to keep the address players already know, so
        the original port is tried first and only moved when something else
        holds it. The search then walks *upwards* from there rather than
        restarting at 2456, so a server imported beside itself lands next to
        the original instead of somewhere unrelated.
        """
        used = [r.config.port for r in self._records.values()]

        def clashes(port: int) -> bool:
            return any(abs(port - other) < 3 for other in used)

        for start in (preferred, 2456):
            port = max(start, 1024)
            while port < 65500:
                if not clashes(port):
                    return port
                port += 3
        raise ValidationError("No free port range is left on this host.")

    def export_archive(self, instance_id: str, *, include_logs: bool = False) -> Path:
        """Write an archive of one whole instance and return the file path."""
        record = self.get(instance_id)
        staging = Path(tempfile.mkdtemp(prefix="vhsm-export-"))
        destination = staging / archive_mod.suggested_filename(record.config)
        return archive_mod.export_instance(
            record.layout, record.config, destination, include_logs=include_logs
        )

    def inspect_archive(self, archive_path: Path) -> archive_mod.ArchiveInfo:
        return archive_mod.read_info(archive_path)

    def import_archive(self, archive_path: Path, *, name: str = "") -> InstanceRecord:
        """Create a new instance from an exported archive.

        The archive keeps its original identity, name and port, none of which
        can be reused blindly: importing onto the same host as the original
        would collide on all three. A fresh id is minted and the name and port
        are moved aside if taken, so importing a server next to itself works.
        """
        info = archive_mod.read_info(archive_path)
        config = InstanceConfig.from_dict(info.config)
        config.id = uuid.uuid4().hex[:12]
        config.name = self._unique_name((name or info.name).strip() or "Imported server")
        config.port = self._free_port(config.port)
        config.created_at = time.time()
        config.validate()

        layout = InstanceLayout.for_instance(self.settings, config.id)
        layout.ensure()
        try:
            archive_mod.extract_into(archive_path, layout.root)
        except archive_mod.ArchiveError:
            shutil.rmtree(layout.root, ignore_errors=True)
            raise
        # The extracted instance.json describes the *original*; ours wins.
        write_json(layout.config_file, config.to_dict())

        record = self._register(config)
        log.info("imported instance %s as %s (%s)", info.name, config.name, config.id)
        return record

    def _clone_config(self, source: InstanceConfig, suffix: str) -> InstanceConfig:
        """A copy of *source*'s configuration with its own identity."""
        config = InstanceConfig.from_dict(source.to_dict())
        config.id = uuid.uuid4().hex[:12]
        config.name = self._unique_name(f"{source.name} {suffix}".strip())
        config.port = self._free_port(source.port)
        config.created_at = time.time()
        config.validate()
        return config

    async def clone(self, instance_id: str) -> InstanceRecord:
        """Duplicate an instance, files and all.

        The directory *is* the instance, so a clone is a copy of it: same
        world, same access lists, same roster, same mods, same snapshots. Only
        the three things that cannot be shared change -- the id, the name and,
        if the original still holds it, the port.

        The copy runs off the event loop because a world can be gigabytes, and
        blocking here would stall every other instance's sampling tick.
        """
        source = self.get(instance_id)
        config = self._clone_config(source.config, "(clone)")

        layout = InstanceLayout.for_instance(self.settings, config.id)
        try:
            await asyncio.to_thread(
                shutil.copytree,
                source.layout.root,
                layout.root,
                # The console transcript belongs to the original's runs, not to
                # the copy, which has not run yet.
                ignore=shutil.ignore_patterns("logs"),
                symlinks=True,
            )
        except OSError as exc:
            shutil.rmtree(layout.root, ignore_errors=True)
            raise ManagerError(f"Could not copy {source.config.name}: {exc}") from exc

        layout.ensure()
        write_json(layout.config_file, config.to_dict())
        record = self._register(config)
        log.info(
            "cloned instance %s as %s (%s) on port %d",
            source.config.name, config.name, config.id, config.port,
        )
        return record

    # ------------------------------------------------------------------ #
    # world backups
    # ------------------------------------------------------------------ #
    def backup_summary(self, instance_id: str) -> dict[str, Any]:
        record = self.get(instance_id)
        root, savedir, world = record.layout.root, record.layout.savedir, record.config.world
        return {
            "world": world,
            "live": backups_mod.live_world(savedir, world),
            "restores": [
                r.to_dict() for r in backups_mod.list_restores(root, savedir, world)
            ],
            "running": record.supervisor.status.is_active,
            "worlds": [w.to_dict() for w in worlds_mod.discover(savedir)],
        }

    def snapshot_world(self, instance_id: str) -> backups_mod.Restore:
        record = self.get(instance_id)
        return backups_mod.snapshot(
            record.layout.root, record.layout.savedir, record.config.world
        )

    def restore_world(self, instance_id: str, key: str) -> backups_mod.Restore:
        """Roll a world back. Refuses while the server is running.

        A running server holds the world in memory and writes it out on its
        own schedule, so it would overwrite whatever was restored underneath
        it at the next autosave.
        """
        record = self.get(instance_id)
        if record.supervisor.status.is_active or record.busy:
            raise backups_mod.BackupError(
                "Stop the server before restoring: a running server would "
                "overwrite the restored world at its next autosave."
            )
        return backups_mod.restore(
            record.layout.root, record.layout.savedir, record.config.world, key
        )

    def delete_backup(self, instance_id: str, key: str) -> None:
        record = self.get(instance_id)
        backups_mod.delete(
            record.layout.root, record.layout.savedir, record.config.world, key
        )

    # ------------------------------------------------------------------ #
    # world import / export
    #
    # A world is the Valheim save on its own -- terrain, structures, the map.
    # Moving one in or out deliberately leaves everything wrapped around it
    # alone: the configuration, the players, the mods and the access lists all
    # stay as they are. Moving the *server* is export_archive / import_archive
    # above.
    # ------------------------------------------------------------------ #
    def export_world_archive(self, instance_id: str) -> Path:
        """Zip the instance's live world and return the file path."""
        record = self.get(instance_id)
        world = worlds_mod.find(record.layout.savedir, record.config.world)
        if world is None:
            raise worlds_mod.WorldError(
                f"{record.config.name} has no world named {record.config.world!r} yet. "
                "One is created the first time the server starts."
            )
        staging = Path(tempfile.mkdtemp(prefix="vhsm-world-export-"))
        stamp = time.strftime("%Y%m%d-%H%M%S")
        destination = staging / f"{slugify(world.name)}-{stamp}.zip"
        return worlds_mod.export_world(world, destination)

    def install_world(
        self,
        instance_id: str,
        staging: Path,
        *,
        name: str = "",
        confirm: bool = False,
        adopt_name: bool = True,
    ) -> "WorldImport":
        """Install an uploaded world into an instance.

        Two cases, and they are genuinely different:

        *No world yet* -- a freshly created instance, or one that has never
        been started. The upload simply becomes the world, and the instance is
        pointed at it: the server loads the world named by ``-world``, which
        for a 1.0 save is the folder name, so an upload whose name differs
        would otherwise be ignored and an empty world generated beside it.

        *A world already there* -- replacing it destroys hours of play, so it
        needs the operator's word first and a snapshot before the fact. The
        upload is installed under the instance's *existing* world name rather
        than its own, which is what makes the replacement a replacement: the
        server keeps loading the same world name, and the snapshot just taken
        sits in the Backups panel beside it as a one-click way back.
        """
        record = self.get(instance_id)
        if record.supervisor.status.is_active or record.busy:
            raise worlds_mod.WorldError(
                "Stop the server before uploading a world: a running server "
                "would overwrite it at its next autosave."
            )

        savedir = record.layout.savedir
        # What the upload calls itself, read before anything is installed, so
        # the UI can say which world went where when the two names differ.
        detected = name.strip() or worlds_mod.identify(staging)[0]
        current = worlds_mod.find(savedir, record.config.world)
        if current is not None:
            if not confirm:
                raise worlds_mod.WorldError(
                    f"{record.config.name} already has a world "
                    f"({record.config.world!r}). Confirm the replacement to import "
                    "over it."
                )
            taken = backups_mod.snapshot(
                record.layout.root, savedir, record.config.world, kind="pre-import"
            )
            installed = worlds_mod.install(
                savedir, staging, name=record.config.world, overwrite=True
            )
            return WorldImport(
                world=installed, source_name=detected, snapshot=taken, replaced=True
            )

        installed = worlds_mod.install(savedir, staging, name=name, overwrite=confirm)
        renamed_from = ""
        if adopt_name and installed.name != record.config.world:
            renamed_from = record.config.world
            self.update(instance_id, {"world": installed.name})
        return WorldImport(
            world=installed, source_name=detected, renamed_from=renamed_from
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
            if running and not restart:
                raise SteamError(
                    "Instances are running and automatic restart is disabled. "
                    "Stop them first, or enable restarting."
                )

            stopped: list[InstanceRecord] = []
            try:
                for record in running:
                    job.log(f"[manager] stopping {record.config.name}")
                    await self.stop(record.config.id)
                    stopped.append(record)

                await install_steamcmd(self.settings, job.log)
                job.log("[manager] running steamcmd app_update 896660")
                async for line in update_server(self.settings, validate=validate):
                    job.log(line)
                write_steam_appid(self.settings)

                self._latest_build = installed_build_id(self.settings) or self._latest_build
                self._latest_checked = time.time()
                self.save_state()
                job.log(f"[manager] installed build {self._latest_build or 'unknown'}")
            finally:
                # Whatever happened above, servers that were running must come
                # back: a failed update is a bad reason to leave them down.
                for record in stopped:
                    job.log(f"[manager] starting {record.config.name}")
                    try:
                        await self.start(record.config.id)
                    except Exception as exc:  # noqa: BLE001
                        job.log(f"[error] could not restart {record.config.name}: {exc}")

        await job.run("update", body)

    async def _auto_snapshot(self) -> None:
        """Take scheduled rollback snapshots for instances that want them."""
        for record in self.records:
            interval = record.config.snapshot_interval
            # Only while running: a stopped server's world does not change, so
            # snapshotting it again would just churn disk.
            if not interval or not record.supervisor.status.is_active:
                continue
            if time.time() - record.last_auto_snapshot < interval * 60:
                continue
            try:
                made = await asyncio.to_thread(
                    backups_mod.snapshot,
                    record.layout.root, record.layout.savedir, record.config.world, "auto",
                )
                record.last_auto_snapshot = time.time()
                dropped = await asyncio.to_thread(
                    backups_mod.prune,
                    record.layout.root, record.config.world, record.config.snapshot_keep, "auto",
                )
                log.info(
                    "auto snapshot %s for %s (pruned %d)",
                    made.key, record.config.name, len(dropped),
                )
            except backups_mod.BackupError as exc:
                # Usually "no world yet"; retry on the next pass rather than
                # spinning on it every few seconds.
                record.last_auto_snapshot = time.time()
                log.warning("auto snapshot skipped for %s: %s", record.config.name, exc)
            except OSError as exc:
                record.last_auto_snapshot = time.time()
                log.warning("auto snapshot failed for %s: %s", record.config.name, exc)

    async def _scheduler_loop(self) -> None:
        """Scheduled updates, automatic snapshots and roster flushes."""
        while True:
            try:
                await self._auto_snapshot()
                for record in self.records:
                    await asyncio.to_thread(record.roster.save)
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
    def _apply_query(self, record: InstanceRecord, info) -> None:
        record.query_version = info.version
        record.players.observe_query(
            info.players, info.max_players, info.player_names, info.player_durations
        )

    async def _query_instance(self, record: InstanceRecord) -> None:
        """Read the Steam query socket, finding it first if we have not yet.

        The socket is not reliably at ``game port + 1`` on every interface, so
        the address is discovered from the process rather than assumed; a
        server that binds to one interface is invisible over loopback.
        """
        endpoint = record.query_endpoint
        if endpoint is not None:
            try:
                info = await a2s_query(endpoint.probe_ip, endpoint.port, A2S_TIMEOUT)
            except A2SError:
                record.query_failures += 1
                # Re-discover rather than keep probing a socket that moved.
                if record.query_failures >= 3:
                    record.query_endpoint = None
                    record.query_failures = 0
                return
            record.query_failures = 0
            self._apply_query(record, info)
            return

        for candidate in query_candidates(record.supervisor.pid, record.config.port)[:4]:
            try:
                info = await a2s_query(candidate.probe_ip, candidate.port, DISCOVER_TIMEOUT)
            except A2SError:
                continue
            log.info(
                "%s: query socket found at %s:%s",
                record.config.name, candidate.probe_ip, candidate.port,
            )
            record.query_endpoint = candidate
            record.query_failures = 0
            self._apply_query(record, info)
            return

    async def probe_instance(self, record: InstanceRecord) -> dict[str, Any]:
        """Everything known about how reachable this server looks from outside."""
        sockets = bound_udp_sockets(record.supervisor.pid)
        payload: dict[str, Any] = {
            "name": record.config.name,
            "status": record.supervisor.status.value,
            "game_port": record.config.port,
            "expected_query_port": record.config.query_port,
            "crossplay": record.config.crossplay,
            "public": record.config.public,
            "log_version": record.supervisor.server_version,
            "sockets": [e.to_dict() for e in sockets],
            "game_port_bound": any(e.port == record.config.port for e in sockets),
            "query_socket_bound": any(e.port != record.config.port for e in sockets),
            "sockets_readable": bool(sockets) or record.supervisor.pid is None,
            "attempts": [],
            "query_ok": False,
            # A silently unresolved steamclient.so dependency looks exactly
            # like a firewall problem from the outside, so it is checked here.
            "libraries": check_libraries(self.settings).to_dict(),
            "notices": record.supervisor.notices,
        }

        for candidate in query_candidates(record.supervisor.pid, record.config.port)[:4]:
            attempt = {"ip": candidate.probe_ip, "port": candidate.port}
            try:
                info = await a2s_query(candidate.probe_ip, candidate.port, DISCOVER_TIMEOUT)
            except A2SError as exc:
                attempt.update({"ok": False, "detail": str(exc)})
                payload["attempts"].append(attempt)
                continue
            attempt["ok"] = True
            payload["attempts"].append(attempt)
            record.query_endpoint = candidate
            payload.update(
                {
                    "query_ok": True,
                    "answered_on": f"{candidate.probe_ip}:{candidate.port}",
                    "query_port": candidate.port,
                    "server_name": info.name,
                    "map": info.map_name,
                    "players": info.players,
                    "max_players": info.max_players,
                    "query_version": info.version,
                    "port_differs": candidate.port != record.config.query_port,
                }
            )
            break
        return payload

    async def sample_once(self) -> dict[str, Any]:
        """Take one sample across every instance and return the broadcast payload."""
        self._tick += 1
        self.net.refresh()

        running = [r for r in self.records if r.supervisor.status.is_active]
        if running and self._tick % QUERY_EVERY_N_TICKS == 0:
            await asyncio.gather(
                *(self._query_instance(r) for r in running), return_exceptions=True
            )
        # The external probe leaves the host, so it runs far less often.
        if self._tick % REACHABILITY_EVERY_N_TICKS == 0:
            await asyncio.gather(
                *(self.check_reachable(r) for r in self.records), return_exceptions=True
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
