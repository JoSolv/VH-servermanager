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
from .discord.config import InstanceDiscord
from .events import EventBus, Event
from . import events as ev
from .instance import InstanceConfig, InstanceLayout, ValidationError
from .mods.profile import ModError, ModProfile
from .mods.thunderstore import ThunderstoreError, ThunderstoreIndex
from .monitor.a2s import A2SError, query as a2s_query
from .monitor.metrics import ProcMetrics, ProcessSampler, host_metrics
from .monitor.net import NetworkMonitor, NetSample
from .monitor.ports import Endpoint, bound_udp_sockets, primary_host_ip, query_candidates
from .monitor.players import LogEvent, PlayerTracker
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
from .util import daily_due, read_json, slugify, write_json

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
        """Whether the daily update should run now.

        Not an exact match on the minute: the loop that asks this also takes
        snapshots and can be inside a long update, so a schedule that only
        came due during one particular minute was a schedule that quietly
        skipped the day whenever that minute was busy.
        """
        return self.enabled and daily_due(self.at, self.last_run_date, now)


@dataclass(slots=True)
class ModState:
    """What this instance's mod set says the operator should know.

    Recomputed from ``mods.json`` and the catalogue only when one of them has
    moved, so the sampling loop can consult it on every tick without reading
    the manifest of every instance twice a second.
    """

    installed: int = 0
    enabled: int = 0
    #: Enabled mods with a newer version on Thunderstore, from
    #: :meth:`ModProfile.updates_available`.
    updates: list[dict[str, str]] = field(default_factory=list)
    #: When the installed mods last changed on disk.
    changed_at: float = 0.0
    #: The (manifest mtime, catalogue fetch time) the rest was computed from.
    key: tuple[float, float] = (0.0, 0.0)


@dataclass(slots=True)
class ModUpdateRun:
    """The outcome of one mod version check, scheduled or asked for."""

    at: float = 0.0
    #: Whether this run was allowed to install what it found.
    install: bool = False
    #: Enabled mods checked.
    checked: int = 0
    #: Updates found, as ``updates_available`` reports them.
    available: list[dict[str, str]] = field(default_factory=list)
    #: What was installed, as ``Name 1.2.3 -> 1.3.0``.
    updated: list[str] = field(default_factory=list)
    restarted: bool = False
    error: str = ""
    #: Why the catalogue could not be refreshed, when the check went ahead
    #: against the copy already on disk anyway.
    stale: str = ""
    #: Set when nothing was actually checked -- there was no catalogue to
    #: check against -- so a scheduled run can come due again instead of
    #: counting as the day's check.
    retry: bool = False

    @property
    def ok(self) -> bool:
        return not self.error

    def summary(self) -> str:
        """One line for the UI, in the operator's terms."""
        if self.error:
            return f"Last check failed: {self.error}"
        if self.updated:
            text = f"Updated {len(self.updated)} mod(s): {', '.join(self.updated)}"
            text += ". The server was restarted." if self.restarted else "."
        elif self.available:
            text = (
                f"{len(self.available)} update(s) available: "
                + ", ".join(
                    f"{entry['name']} {entry['current']} \u2192 {entry['latest']}"
                    for entry in self.available
                )
                + "."
            )
            if not self.install:
                text += " This check only reports, so nothing was installed."
        else:
            text = f"Checked {self.checked} mod(s); everything is up to date."
        if self.stale:
            text += (
                " Thunderstore could not be reached, so this used the "
                f"catalogue already here ({self.stale})."
            )
        return text

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "install": self.install,
            "checked": self.checked,
            "available": self.available,
            "updated": self.updated,
            "restarted": self.restarted,
            "error": self.error,
            "stale": self.stale,
            "summary": self.summary() if self.at else "",
        }


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
    #: Mod updates and the state of the mods on disk, refreshed by the manager.
    mods: ModState = field(default_factory=ModState)
    #: The last mod version check, scheduled or asked for by hand.
    mod_check: ModUpdateRun = field(default_factory=ModUpdateRun)
    #: Whether a mod check is in flight. A check installs files into the
    #: profile, so two at once would be two writers to the same mod folder.
    mod_check_busy: bool = False
    #: Non-empty while the manager itself is taking this instance down and
    #: back up for maintenance -- a server update, or a mod update that
    #: installed something. Players are not told about those stops and starts:
    #: they get one message about what changed once it is back, which is the
    #: part they care about. A restart somebody asked for by hand is not
    #: maintenance and is announced.
    maintenance: str = ""
    #: The enabled mod set this instance was last seen *running*, as
    #: ``{package: version}``. None until the first time it is seen running,
    #: so a manager restart does not report every mod as new.
    announced_mods: dict[str, str] | None = None
    #: The ``ModState.changed_at`` that :attr:`announced_mods` was read at, so
    #: the comparison re-reads the manifest only when it has actually moved
    #: rather than on every sampling tick.
    announced_mods_at: float = 0.0
    #: The dedicated-server build this instance was last seen running on.
    #: None until first seen, for the same reason.
    announced_build: str | None = None

    @property
    def busy(self) -> bool:
        return self.operation is not None and not self.operation.done()

    @property
    def version(self) -> str:
        return self.supervisor.server_version or self.query_version

    @property
    def restart_pending(self) -> bool:
        """Whether this server is running an older mod set than is on disk.

        A server reads its plugins once, at start-up, so mods installed,
        updated, or switched on or off since then are sitting in the profile
        unused -- and nothing in the console says so, because from the
        server's side nothing happened.
        """
        started = self.supervisor.started_at
        return bool(
            started
            and self.config.mods_enabled
            and self.mods.changed_at
            and self.mods.changed_at > started
        )

    @property
    def mod_notices(self) -> list[dict[str, Any]]:
        """What the mods on this instance have to say, as notices.

        Shaped like the supervisor's own notices so the instance page and the
        live patcher render both the same way, and carried on every snapshot:
        a release lands, or a mod is installed from another tab, while the
        page someone is looking at just sits there.
        """
        notices: list[dict[str, Any]] = []
        updates = self.mods.updates
        if updates:
            listed = ", ".join(
                f"{entry['name']} {entry['current']} \u2192 {entry['latest']}"
                for entry in updates
            )
            notices.append(
                {
                    # The versions ride in the key so that the client, which
                    # compares keys to decide whether anything changed, sees
                    # news about a different release as a different notice.
                    "key": "mod-updates:"
                    + ",".join(
                        f"{entry['package_full_name']}-{entry['latest']}"
                        for entry in updates
                    ),
                    "level": "info",
                    "title": (
                        f"{len(updates)} of this server's mods have a newer "
                        "version on Thunderstore."
                        if len(updates) > 1
                        else "One of this server's mods has a newer version on "
                        "Thunderstore."
                    ),
                    "detail": listed,
                    "link": {
                        "href": f"/instances/{self.config.id}/mods",
                        "label": "Manage mods",
                    },
                }
            )
        if self.restart_pending:
            notices.append(
                {
                    "key": "mods-need-restart",
                    "level": "important",
                    "title": (
                        "Mods have changed since this server started, so it is "
                        "not running them yet."
                    ),
                    "detail": (
                        "A server loads its plugins once, when it starts. "
                        "Restart it to pick up what has been installed, "
                        "updated, enabled or disabled since."
                    ),
                }
            )
        return notices

    @property
    def notices(self) -> list[dict[str, Any]]:
        """Everything worth telling the operator about this instance."""
        return self.supervisor.notices + self.mod_notices

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
            # Known failures recognised in this run, plus what the mods on
            # disk have to say. Carried on every tick so an open page learns
            # about one without being reloaded -- which matters, because these
            # are raised while a server is running.
            "notices": self.notices,
            "version": self.version,
            "reachable": self.reachable,
            "listening": self.listening,
            "maintenance": self.maintenance,
            # Whether the Steam query socket has answered for this process.
            # Like `listening` it is evidence the server is actually up, but
            # it is refreshed every few seconds rather than every half minute,
            # so it is the signal worth acting on promptly.
            "query_answered": self.query_endpoint is not None,
            "reachable_at": self.reachable_at,
            "reachable_detail": self.reachable_detail,
            "players": self.players.to_dict(self.lists),
            "metrics": self.metrics.to_dict(),
            "net": self.net.to_dict(),
            "mods": {
                "installed": self.mods.installed,
                "enabled": self.mods.enabled,
                # Named as ModProfile.summary() names it, because the instance
                # endpoint merges that over this and the two must agree.
                "updates_active": self.mods.updates,
                "changed_at": self.mods.changed_at,
                "restart_pending": self.restart_pending,
                "scheduled": config.mod_update_check,
                "last_check": self.mod_check.to_dict(),
            },
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
        #: Things that happened, for anything that wants telling. The Hub
        #: above carries *state* for a browser to render; this carries
        #: *changes*, which is the shape a notifier needs. Nothing here
        #: knows what subscribes -- today it is the optional Discord bot.
        self.events = EventBus()
        #: Optional: something that diffs each sample and publishes what
        #: moved. None until a subscriber attaches one.
        self._watcher: Any = None
        self.net = NetworkMonitor()
        self.job = JobLog(self.settings.data_root / "steamcmd.log")
        self._proc_sampler = ProcessSampler()
        self._records: dict[str, InstanceRecord] = {}
        self._sampler_task: asyncio.Task[None] | None = None
        self._scheduler_task: asyncio.Task[None] | None = None
        self._index_task: asyncio.Task[None] | None = None
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
        # rather than by parsing the log a second time. Two things want them
        # now, so they fan out here: the roster, which is the record of who
        # was here, and the bus, for whoever wants telling as it happens.
        record.players = PlayerTracker(
            on_event=lambda event: self._player_event(record, event)
        )
        supervisor.add_log_hook(record.players.observe_log)
        self._records[config.id] = record
        self.net.watch(config.port)
        return record

    def _player_event(self, record: InstanceRecord, event: LogEvent) -> None:
        """One resolved player event: to the roster, then onto the bus.

        The roster comes first and its failure is not allowed to cost the
        announcement, or the other way round: these are two independent
        consumers of one event, not a chain.
        """
        try:
            record.roster.observe(event)
        except Exception:  # noqa: BLE001 - a listener must never break log reading
            log.exception("roster could not record a player event")

        name = event.name or event.player_id or "Someone"
        server = record.config.name
        # The marks and wording follow vhsm.discord.render's vocabulary, so a
        # join reads the same as the roster row it will become.
        if event.kind == "named":
            self.events.publish(Event(
                kind=ev.PLAYER_JOIN,
                instance_id=record.config.id, instance_name=server,
                title=f"\U0001F7E2 **{name}** joined {server}.",
                level="good",
                data={"player": name, "player_id": event.player_id},
            ))
        elif event.kind == "disconnect":
            # A session that never got as far as a character name was somebody
            # connecting and failing, which is not a player leaving.
            if not event.name:
                return
            played = f" after {int((event.count or 0) / 60)}m" if event.count else ""
            self.events.publish(Event(
                kind=ev.PLAYER_LEAVE,
                instance_id=record.config.id, instance_name=server,
                title=f"\u26AB **{name}** left {server}{played}.",
                level="muted",
                data={"player": name, "player_id": event.player_id,
                      "playtime": event.count},
            ))
        elif event.kind == "died":
            self.events.publish(Event(
                kind=ev.PLAYER_DEATH,
                instance_id=record.config.id, instance_name=server,
                title=f"\U0001F480 **{name}** died on {server}.",
                level="important",
                data={"player": name, "player_id": event.player_id},
            ))

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
    # mod state: updates available, and whether a restart is owed
    # ------------------------------------------------------------------ #
    def refresh_mod_state(self, instance_id: str) -> ModState:
        """Re-read one instance's mod set now, whatever the cache says.

        Called after anything that changes mods, so the page that made the
        change does not have to wait for the next sampling tick to be told
        about it.
        """
        return self._refresh_mod_state(self.get(instance_id), force=True)

    def _refresh_mod_state(self, record: InstanceRecord, *, force: bool = False) -> ModState:
        state = record.mods
        manifest = record.layout.mods_manifest
        try:
            mtime = manifest.stat().st_mtime
        except OSError:
            mtime = 0.0
        key = (mtime, self.index.fetched_at)
        if not force and state.key == key:
            return state

        if not mtime:
            # No manifest at all: nothing has ever been installed here.
            state.installed = state.enabled = 0
            state.updates = []
            state.changed_at = 0.0
            state.key = key
            return state

        profile = ModProfile(record.layout, self.settings.cache_dir, self.index)
        mods = profile.mods
        state.installed = len(mods)
        state.enabled = sum(1 for mod in mods if mod.enabled)
        state.updates = profile.updates_available(enabled_only=True)
        state.changed_at = profile.changed_at
        state.key = key
        return state

    def ensure_index_background(self) -> None:
        """Start loading the Thunderstore catalogue, without waiting for it.

        The update notices need the catalogue, and rendering a page is the
        wrong place to fetch it: it is the whole community index in one
        response, so on a cold start somebody would sit in front of a blank
        page for it. The notices ride the metrics socket, so they arrive a
        tick after the load finishes instead.
        """
        if self._index_task is not None and not self._index_task.done():
            return
        if self.index.loaded and self.index.age < self.settings.index_ttl:
            return

        async def load() -> None:
            try:
                await self.index.ensure()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - nobody awaits this task
                # Including a disk that cannot hold the cache: an unretrieved
                # exception here would surface as a bare asyncio complaint at
                # shutdown rather than as a line anyone can act on.
                log.warning("catalogue load failed: %s", exc)

        self._index_task = asyncio.create_task(load())

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

    def _assert_channel_free(self, config: InstanceConfig, *, exclude: str = "") -> None:
        """No two instances may share a Discord channel.

        The channel is how a command is matched to a server, so two instances
        on one channel would make every command there ambiguous. Refused at
        save time rather than resolved arbitrarily at command time.
        """
        channel = config.discord.channel_id
        if not channel or not config.discord.enabled:
            return
        for record in self._records.values():
            if record.config.id == exclude:
                continue
            other = record.config.discord
            if other.enabled and other.channel_id == channel:
                raise ValidationError(
                    f"{record.config.name!r} already uses that Discord channel. "
                    "Each server needs its own: the channel is what tells the "
                    "bot which server a command is about."
                )

    def create(self, config: InstanceConfig) -> InstanceRecord:
        config.validate()
        self._assert_port_free(config.port)
        self._assert_channel_free(config)
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
        self._assert_channel_free(updated, exclude=instance_id)

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

    def submit(
        self, instance_id: str, action: str, *, actor: str = "web"
    ) -> InstanceRecord:
        """Run a lifecycle action as a manager-owned background task.

        *actor* is who asked -- ``web`` for a button in the GUI, or
        ``discord:<name>`` for a command. It rides into the log and onto the
        event bus so that "who restarted the server" has an answer, which is
        the first thing anyone asks once something other than the GUI can do
        it.

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
        log.info("%s: %s requested by %s", record.config.name, action, actor)
        self.events.publish(Event(
            kind=ev.LIFECYCLE,
            instance_id=record.config.id,
            instance_name=record.config.name,
            title=f"{record.config.name}: {action} requested by {actor}.",
            level="info",
            data={"action": action, "actor": actor},
        ))
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
        # The archive names a Discord server and channel belonging to whoever
        # exported it, which this operator can neither see nor be assumed to
        # want posted to. Identity and port are already reassigned for the
        # same reason, so the whole Discord section starts blank.
        config.discord = InstanceDiscord()
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

        build = {
            "installed": installed,
            "latest": self._latest_build,
            "checked_at": self._latest_checked,
            # Only claim an update when both ids are known and differ.
            "available": bool(installed and self._latest_build and installed != self._latest_build),
        }
        if self._watcher is not None:
            # Every page that shows the build badge calls this, so the watcher
            # hears about a new build without a timer of its own.
            self._watcher.observe_build(build)
        return build

    async def run_update(self, *, validate: bool = True, restart: bool = True) -> None:
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
                    # A restart the operator started a moment ago owns this
                    # instance's process; let go of it before taking it down,
                    # or the two take turns starting and stopping it.
                    await self._cancel_operation(record)
                    # Players are told what changed when it comes back, not
                    # that it went down for the update.
                    record.maintenance = "update"
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
                    if not record.supervisor.status.is_active:
                        # It did not come back, so there is nothing to wait
                        # for. Otherwise the flag is cleared by the sampler
                        # once the server is reachable again, which is the
                        # moment the players would otherwise have been told
                        # it was online.
                        record.maintenance = ""

        before = installed_build_id(self.settings)
        ok = await job.run("update", body)
        after = installed_build_id(self.settings)
        if ok and after and after != before:
            self.events.publish(Event(
                kind=ev.SERVER_UPDATED,
                title="The Valheim dedicated server was updated.",
                detail=(
                    f"Build `{before or 'unknown'}` \u2192 `{after}`."
                    + (" Running servers were restarted." if restart else "")
                ),
                level="good",
                key=f"server-updated:{after}",
            ))
        elif not ok:
            self.events.publish(Event(
                kind=ev.SERVER_UPDATED,
                title="The Valheim dedicated server update failed.",
                detail="The steamcmd transcript is in Settings.",
                level="bad",
                key=f"server-update-failed:{int(time.time())}",
            ))

    async def maybe_run_scheduled_update(self, now: datetime | None = None) -> bool:
        """Run the daily server update if it is due. Says whether it ran.

        Separate from the loop so the schedule can be exercised directly
        rather than by waiting for a particular minute to come round.
        """
        now = now or datetime.now()
        if not self.auto_update.due(now) or self.job.running:
            return False
        log.info("scheduled update starting (%s)", self.auto_update.at)
        # Recorded before the work, not after: the update stops and starts
        # servers and takes as long as a multi-GB download takes, and a
        # second pass starting in the middle of that would be a second
        # update.
        self.auto_update.last_run_date = now.strftime("%Y-%m-%d")
        self.save_state()
        await self.run_update(
            validate=self.auto_update.validate,
            restart=self.auto_update.restart_instances,
        )
        return True

    # ------------------------------------------------------------------ #
    # mod updates, per instance
    # ------------------------------------------------------------------ #
    async def run_mod_update(
        self,
        instance_id: str,
        *,
        install: bool | None = None,
        restart: bool | None = None,
    ) -> ModUpdateRun:
        """Check one instance's mods against Thunderstore and act on it.

        *install* and *restart* default to the instance's own settings, so a
        check asked for by hand does exactly what the schedule would do.
        Only the mods the server actually loads are considered: a disabled
        mod is not running, and updating it behind the operator's back is a
        change to a server nobody asked to change.
        """
        record = self.get(instance_id)
        config = record.config
        install = config.mod_update_install if install is None else install
        restart = config.mod_update_restart if restart is None else restart

        if record.mod_check_busy:
            raise ManagerError(f"A mod check for {config.name} is already running.")

        run = ModUpdateRun(at=time.time(), install=install)
        profile = ModProfile(record.layout, self.settings.cache_dir, self.index)
        run.checked = sum(1 for mod in profile.mods if mod.enabled)
        record.mod_check_busy = True
        try:
            result = await self._run_mod_update(record, profile, run, install, restart)
        finally:
            record.mod_check_busy = False
        # Announced here rather than inside the run, so a check asked for by
        # hand and the nightly one report identically.
        if not result.retry:
            self._announce_mod_check(record, result)
        return result

    async def _run_mod_update(
        self,
        record: InstanceRecord,
        profile: ModProfile,
        run: ModUpdateRun,
        install: bool,
        restart: bool,
    ) -> ModUpdateRun:
        config = record.config
        try:
            # Forced: a scheduled check is precisely the moment the cached
            # catalogue is too old to answer from, and reading a day-old copy
            # would report yesterday's releases as today's news.
            await self.index.ensure(force=True)
        except ThunderstoreError as exc:
            if not self.index.loaded:
                # No catalogue at all, so there is nothing to compare against
                # and nothing to report: leave the day's check undone.
                run.error = str(exc)
                run.retry = True
                record.mod_check = run
                log.warning(
                    "mod check for %s has no catalogue to check against: %s",
                    config.name, exc,
                )
                return run
            # A catalogue that could not be refreshed is still a catalogue.
            # Checking the copy on disk and saying it is not fresh beats
            # reporting nothing because Thunderstore was briefly away.
            run.stale = str(exc)
            log.warning("mod check for %s is using a stale catalogue: %s", config.name, exc)

        run.available = [dict(entry) for entry in profile.updates_available(enabled_only=True)]
        if install:
            for entry in run.available:
                try:
                    installed = await profile.install_by_name(
                        entry["package_full_name"], entry["latest"]
                    )
                except (ModError, ThunderstoreError, OSError) as exc:
                    # Report the one that failed and stop: the rest can wait
                    # for the next pass rather than be attempted while
                    # something about this profile is clearly wrong.
                    run.error = f"{entry['package_full_name']}: {exc}"
                    break
                run.updated.append(
                    f"{entry['name']} {entry['current']} \u2192 {entry['latest']}"
                )
                # A new release can need a dependency the old one did not.
                # Report that too: changing two mods and naming one is how a
                # report stops being worth reading.
                run.updated.extend(
                    f"{mod.name} {mod.version} (new dependency)"
                    for mod in installed
                    if mod.package_full_name.lower() != entry["package_full_name"].lower()
                )

        if run.updated and restart and record.supervisor.status.is_active:
            try:
                await self._cancel_operation(record)
                # Players hear "now running these mods" when it is back up,
                # rather than a stop and a start they did not ask for.
                record.maintenance = "mod-update"
                await self.restart(config.id)
                run.restarted = True
            except Exception as exc:  # noqa: BLE001 - reported on the page
                run.error = f"{run.error + '; ' if run.error else ''}restart failed: {exc}"
                log.warning("restart after mod update failed for %s: %s", config.name, exc)
            if not record.supervisor.status.is_active:
                record.maintenance = ""

        record.mod_check = run
        self._refresh_mod_state(record, force=True)
        log.info("mod check for %s: %s", config.name, run.summary())
        return run

    def _announce_mod_check(self, record: InstanceRecord, run: "ModUpdateRun") -> None:
        """Report what a mod check did, in the words the GUI already uses."""
        self.events.publish(Event(
            kind=ev.MOD_CHECK,
            instance_id=record.config.id,
            instance_name=record.config.name,
            title=f"{record.config.name}: mod check",
            detail=run.summary(),
            level="bad" if run.error else ("good" if run.updated else "info"),
            # Keyed by the run, not by its outcome: two checks that found the
            # same thing are two pieces of news, because the second one means
            # it is still true a day later.
            key=f"mod-check:{record.config.id}:{int(run.at)}",
            data={"run": run.to_dict()},
        ))

    async def _auto_mod_updates(self) -> None:
        """Run the per-instance mod checks that have come due."""
        now = datetime.now()
        for record in self.records:
            config = record.config
            if not config.mod_update_check:
                continue
            if not daily_due(config.mod_update_at, config.mod_update_last_run, now):
                continue
            previous = config.mod_update_last_run
            # Marked before the check for the same reason as the server
            # update above, and put back below if nothing was checked.
            config.mod_update_last_run = now.strftime("%Y-%m-%d")
            self.save(config.id)
            log.info(
                "scheduled mod check for %s (%s)", config.name, config.mod_update_at
            )
            try:
                run = await self.run_mod_update(config.id)
            except ManagerError as exc:
                log.warning("scheduled mod check skipped for %s: %s", config.name, exc)
                continue
            if run.retry:
                # Nothing was checked, so the day's check has not happened:
                # leave it due, and the rest of the catch-up window is spent
                # retrying rather than waiting for tomorrow.
                config.mod_update_last_run = previous
                self.save(config.id)

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
                self.events.publish(Event(
                    kind=ev.SNAPSHOT,
                    instance_id=record.config.id,
                    instance_name=record.config.name,
                    title=f"{record.config.name}: world snapshot taken.",
                    detail=f"{made.key}"
                           + (f", {len(dropped)} pruned" if dropped else ""),
                    level="info",
                    key=f"snapshot:{record.config.id}:{made.key}",
                ))
            except backups_mod.BackupError as exc:
                # Usually "no world yet"; retry on the next pass rather than
                # spinning on it every few seconds.
                record.last_auto_snapshot = time.time()
                log.warning("auto snapshot skipped for %s: %s", record.config.name, exc)
            except OSError as exc:
                record.last_auto_snapshot = time.time()
                log.warning("auto snapshot failed for %s: %s", record.config.name, exc)

    async def _scheduler_loop(self) -> None:
        """Scheduled updates and mod checks, snapshots and roster flushes."""
        while True:
            try:
                await self._auto_snapshot()
                for record in self.records:
                    await asyncio.to_thread(record.roster.save)
                await self._auto_mod_updates()
                await self.maybe_run_scheduled_update()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduled work failed")
            # Both schedules have an hour of slack, so this interval decides
            # how promptly they start rather than whether they run at all.
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

        # Read from the app manifest at most once per tick, and only if some
        # instance is actually up to compare it against.
        build_id: str | None = None
        snapshots: list[dict[str, Any]] = []

        for record in self.records:
            # Cheap: a stat() per instance, and a re-read only when the
            # manifest or the catalogue has actually moved.
            self._refresh_mod_state(record)
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

            # Taken before the call below, which ends a maintenance window:
            # the suppression reads this snapshot, so clearing the flag first
            # would announce the very start-up it is there to hold back.
            snapshots.append(record.snapshot())

            # What changed since this server last came up, once it is up and
            # actually running it. The build id is read once per tick rather
            # than once per instance: they all share one game install.
            try:
                if build_id is None:
                    build_id = installed_build_id(self.settings)
                self._announce_what_changed(record, build_id)
            except Exception:  # noqa: BLE001 - never break the sampling tick
                log.exception("could not work out what changed for %s", record.config.name)

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
            "instances": snapshots,
        }

    def _announce_what_changed(self, record: InstanceRecord, build: str) -> None:
        """Tell this server's players what is different now that it is up.

        Both halves of this answer the same question -- "what changed?" -- by
        comparing what the server is *running* against what it was running the
        last time it came up. That is why they are here rather than at the
        moment the install happened: a mod sitting on disk unloaded, or a build
        installed while the server was down, is not news to a player yet. It
        becomes news when they can join and find it there.

        Nothing is said until the server is reachable and has actually loaded
        what is on disk, and the first sighting only records the baseline, so a
        manager restart does not report everything as new.
        """
        connectable = record.supervisor.status.is_active and (
            record.listening or record.query_endpoint is not None
        )
        # restart_pending means the files moved but this process has not read
        # them, which is exactly the case where the players are not running it.
        if not connectable or record.restart_pending:
            return

        # It is back up, so the maintenance window is over. Cleared here --
        # after this tick's snapshot has been taken, which is what the
        # suppression reads -- so the "online" the players would have been
        # sent is the one that gets held back, and what they get instead is
        # whatever changed, below.
        record.maintenance = ""

        # Reading the manifest twice a second per instance would undo the care
        # _refresh_mod_state takes, so the mod set is only re-read when the
        # state it was derived from has moved -- or when there is no baseline.
        changed_at = record.mods.changed_at
        if record.announced_mods is None or changed_at != record.announced_mods_at:
            profile = ModProfile(record.layout, self.settings.cache_dir, self.index)
            enabled = [m for m in profile.mods if m.enabled] \
                if record.config.mods_enabled else []
            running = {mod.package_full_name: mod.version for mod in enabled}
            # The display name as the mod itself reports it, so an
            # announcement names it exactly as /vh mods does rather than
            # guessing from the package id.
            names = {mod.package_full_name: mod.name for mod in enabled}
            previous = record.announced_mods
            record.announced_mods = running
            record.announced_mods_at = changed_at
            if previous is not None and running != previous:
                self._announce_mod_diff(record, previous, running, names)

        previous_build, record.announced_build = record.announced_build, build
        if build and previous_build is not None and build != previous_build:
            self.events.publish(Event(
                kind=ev.UPDATED,
                instance_id=record.config.id,
                instance_name=record.config.name,
                title=f"\u2B06\uFE0F {record.config.name} has been updated",
                # The Valheim version is what a player has to match to join;
                # the Steam build id it came from is operator business.
                detail=(
                    f"Now running Valheim `{record.version}`."
                    if record.version else "The server files were updated."
                ),
                level="info",
                key=f"updated:{record.config.id}:{build}",
                data={"build": build, "version": record.version},
            ))

    def _announce_mod_diff(
        self,
        record: InstanceRecord,
        previous: dict[str, str],
        running: dict[str, str],
        names: dict[str, str],
    ) -> None:
        """One message naming what this server's mod set gained, lost or moved.

        Laid out like ``/vh mods``: a bullet and the mod's own name, with
        versions in code. A mod that has gone has no name to look up any more,
        so the package id is split as a last resort.
        """
        def label(package: str) -> str:
            # "Author-Mod" reads as the mod's name to a player, not the
            # author's, which is the right fallback for one no longer here.
            return names.get(package) or package.split("-", 1)[-1]

        lines = [
            f"\u2022 {label(package)} `{previous[package]}` \u2192 `{version}`"
            for package, version in running.items()
            if package in previous and previous[package] != version
        ]
        lines += [
            f"\u2022 {label(package)} `{version}` (new)"
            for package, version in running.items()
            if package not in previous
        ]
        gone = [label(package) for package in previous if package not in running]
        if gone:
            lines.append(f"\u2022 removed: {', '.join(gone)}")
        if not lines:
            return
        self.events.publish(Event(
            kind=ev.MODS_CHANGED,
            instance_id=record.config.id,
            instance_name=record.config.name,
            # The mods mark, and no markdown: this is an embed title.
            title=f"\U0001F4E6 {record.config.name} is running updated mods",
            detail="\n".join(lines),
            level="info",
            key=f"mods-changed:{record.config.id}:{sorted(running.items())}",
            data={"mods": running},
        ))

    async def _sampler_loop(self) -> None:
        interval = self.settings.sample_interval
        while True:
            try:
                payload = await self.sample_once()
                self.hub.publish(payload)
                if self._watcher is not None:
                    # Diffing is a dictionary comparison per instance; it runs
                    # here rather than in the notifier so that every notifier
                    # sees the same events from the same samples.
                    self._watcher.observe(payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("sampler tick failed")
            await asyncio.sleep(interval)

    def attach_watcher(self, watcher: Any) -> None:
        """Have *watcher* see every sample, so it can publish what changed.

        Kept as an attachment rather than built in because deriving events
        from the snapshot stream is only worth doing when something is
        listening for them.
        """
        self._watcher = watcher

    def start_background(self) -> None:
        if self._sampler_task is None or self._sampler_task.done():
            self._sampler_task = asyncio.create_task(self._sampler_loop())
        if self._scheduler_task is None or self._scheduler_task.done():
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    async def stop_background(self) -> None:
        for attr in ("_sampler_task", "_scheduler_task", "_index_task"):
            task = getattr(self, attr)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                setattr(self, attr, None)
