"""Deriving events from manager state, and posting them to the right channel.

Two halves:

:class:`StateWatcher`
    Diffs the snapshot the sampling loop already produces and publishes an
    :class:`~vhsm.events.Event` when something actually changed. This is where
    "the server is running" becomes "the server started", which is the only
    form worth announcing.

:class:`Announcer`
    Decides whether an event is announced at all, which channel it goes to,
    and what it reads like.

Both are deliberately conservative about saying anything. A notifier that
cries wolf gets muted, and a muted notifier is worse than none -- so a
flapping probe is held until it settles, and nothing is announced merely
because the manager restarted and saw the world afresh.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .. import events as ev
from ..events import Event
from . import render
from .config import DiscordConfig

log = logging.getLogger("vhsm.discord.announce")

#: How long after start-up to watch quietly. Everything is a "change" on the
#: first tick -- every stopped server, every pending mod update -- and a
#: manager restart must not read as twenty things happening at once.
SETTLE_SECONDS = 20.0

#: Consecutive disagreeing samples before a reachability change is believed.
#: The probe is a UDP round trip over the operator's own router; one lost
#: packet is not an outage.
REACHABILITY_CONFIRMATIONS = 3


@dataclass(slots=True)
class InstanceState:
    """What we last saw, per instance, so we can tell what moved."""

    status: str = ""
    listening: bool = False
    version: str = ""
    reachable: bool | None = None
    #: Candidate reachability and how many samples have agreed on it.
    pending_reachable: bool | None = None
    pending_count: int = 0
    notice_keys: set[str] = field(default_factory=set)
    mod_update_key: str = ""
    restart_pending: bool = False
    announced_ready: bool = False


class StateWatcher:
    """Turns the snapshot stream into events.

    Fed from the sampling loop, which already walks every instance twice a
    second; this adds a dictionary comparison per instance to it rather than
    any new monitoring.
    """

    def __init__(self, bus: ev.EventBus) -> None:
        self._bus = bus
        self._instances: dict[str, InstanceState] = {}
        self._started_at = time.monotonic()
        self._build_update_key = ""

    @property
    def settling(self) -> bool:
        """Whether we are still in the quiet period after start-up."""
        return time.monotonic() - self._started_at < SETTLE_SECONDS

    def observe(self, snapshot: dict[str, Any]) -> None:
        """Compare one broadcast payload against the last one."""
        for instance in snapshot.get("instances") or ():
            try:
                self._observe_instance(instance)
            except Exception:  # noqa: BLE001 - never break the sampling loop
                log.exception("discord: could not diff instance state")
        seen = {i.get("id") for i in snapshot.get("instances") or ()}
        for gone in set(self._instances) - seen:
            self._instances.pop(gone, None)

    def observe_build(self, build: dict[str, Any]) -> None:
        """Note whether a newer dedicated-server build has been published."""
        if not build.get("available"):
            self._build_update_key = ""
            return
        key = f"build:{build.get('latest')}"
        if key == self._build_update_key:
            return
        self._build_update_key = key
        if self.settling:
            return
        self._publish(
            ev.SERVER_UPDATE,
            title="A newer Valheim dedicated-server build has been published.",
            detail=(
                f"Installed `{build.get('installed') or 'unknown'}`, "
                f"newest `{build.get('latest')}`."
            ),
            level="important",
            key=key,
        )

    # ------------------------------------------------------------------ #
    def _observe_instance(self, snapshot: dict[str, Any]) -> None:
        instance_id = str(snapshot.get("id") or "")
        if not instance_id:
            return
        name = str(snapshot.get("name") or "")
        first_sight = instance_id not in self._instances
        state = self._instances.setdefault(instance_id, InstanceState())
        status = str(snapshot.get("status") or "stopped")

        # The first sight of an instance is not news about it, and neither is
        # anything during the settling period: both are the manager learning
        # the state, not the state changing.
        quiet = first_sight or self.settling

        if status != state.status:
            previous, state.status = state.status, status
            if status != "running":
                state.announced_ready = False
            if not quiet:
                self._status_change(instance_id, name, previous, status, snapshot)

        # "Running" is the process existing; "ready" is a socket players can
        # actually reach. Either signal will do: the kernel saying the process
        # holds its game port, or the Steam query socket having answered. The
        # first is the stronger fact and the second arrives sooner -- it is
        # re-checked every few seconds against every half minute -- and a
        # "your server is up" that lands thirty seconds late is a poor
        # announcement. Announced once per run.
        ready = status == "running" and bool(
            snapshot.get("listening") or snapshot.get("query_answered")
        )
        if ready and not state.announced_ready:
            state.announced_ready = True
            if not quiet:
                self._publish(
                    ev.READY,
                    instance_id=instance_id,
                    instance_name=name,
                    title=f"{name} is up and accepting players.",
                    detail=(
                        f"Version {snapshot['version']}"
                        if snapshot.get("version") else ""
                    ),
                    level="good",
                    key=f"ready:{instance_id}:{snapshot.get('uptime')}",
                    data={"snapshot": snapshot},
                )
        state.listening = bool(snapshot.get("listening"))
        state.version = str(snapshot.get("version") or "")

        self._observe_reachability(instance_id, name, snapshot, state, quiet)
        self._observe_notices(instance_id, name, snapshot, state, quiet)
        self._observe_mods(instance_id, name, snapshot, state, quiet)

    def _status_change(
        self,
        instance_id: str,
        name: str,
        previous: str,
        status: str,
        snapshot: dict[str, Any],
    ) -> None:
        if status == "crashed":
            exit_code = snapshot.get("exit_code")
            detail = snapshot.get("last_error") or ""
            if exit_code is not None:
                detail = (f"Exit code {exit_code}. " + detail).strip()
            self._publish(
                ev.CRASH,
                instance_id=instance_id,
                instance_name=name,
                title=f"{name} stopped unexpectedly.",
                detail=detail,
                level="bad",
                key=f"crash:{instance_id}:{exit_code}",
                data={"snapshot": snapshot},
            )
            return

        wording = {
            "starting": f"{name} is starting.",
            "running": f"{name} is running.",
            "stopping": f"{name} is shutting down.",
            "stopped": f"{name} has stopped.",
        }.get(status, f"{name} is {status}.")
        self._publish(
            ev.STATUS,
            instance_id=instance_id,
            instance_name=name,
            title=wording,
            level=render.STATUS_LEVELS.get(status, "info"),
            key=f"status:{instance_id}:{status}",
            data={"from": previous, "to": status, "snapshot": snapshot},
        )

    def _observe_reachability(
        self,
        instance_id: str,
        name: str,
        snapshot: dict[str, Any],
        state: InstanceState,
        quiet: bool,
    ) -> None:
        """Announce a change only once several samples agree on it.

        ``None`` means the question does not apply -- the server is stopped,
        or it is a crossplay server with no query socket -- and is never
        announced: "we cannot tell" is not an outage.
        """
        reachable = snapshot.get("reachable")
        if reachable is None:
            state.pending_reachable, state.pending_count = None, 0
            state.reachable = None
            return
        if reachable == state.reachable:
            state.pending_reachable, state.pending_count = None, 0
            return

        if reachable != state.pending_reachable:
            state.pending_reachable, state.pending_count = reachable, 1
            return
        state.pending_count += 1
        if state.pending_count < REACHABILITY_CONFIRMATIONS:
            return

        was, state.reachable = state.reachable, reachable
        state.pending_reachable, state.pending_count = None, 0
        # Nothing to say the first time we form an opinion; the news is a
        # change of opinion.
        if quiet or was is None:
            return
        if reachable:
            self._publish(
                ev.REACHABILITY,
                instance_id=instance_id, instance_name=name,
                title=f"{name} is reachable from outside again.",
                level="good", key=f"reach:{instance_id}:up",
            )
        else:
            self._publish(
                ev.REACHABILITY,
                instance_id=instance_id, instance_name=name,
                title=f"{name} is not answering from its public address.",
                detail=snapshot.get("reachable_detail") or "",
                level="important", key=f"reach:{instance_id}:down",
            )

    def _observe_notices(
        self,
        instance_id: str,
        name: str,
        snapshot: dict[str, Any],
        state: InstanceState,
        quiet: bool,
    ) -> None:
        """Announce recognised failures, each one once.

        The notices already carry a stable key for exactly this purpose, and
        the mod ones are handled separately so they can be switched on and
        off on their own.
        """
        notices = [
            notice for notice in snapshot.get("notices") or ()
            if not str(notice.get("key", "")).startswith(("mod-updates:", "mods-need-restart"))
        ]
        keys = {str(notice.get("key") or notice.get("title")) for notice in notices}
        fresh = keys - state.notice_keys
        state.notice_keys = keys
        if quiet:
            return
        for notice in notices:
            key = str(notice.get("key") or notice.get("title"))
            if key not in fresh:
                continue
            self._publish(
                ev.NOTICE,
                instance_id=instance_id,
                instance_name=name,
                title=str(notice.get("title") or "Something needs attention."),
                detail=str(notice.get("detail") or ""),
                level="bad" if notice.get("level") == "error" else "important",
                key=f"notice:{instance_id}:{key}",
            )

    def _observe_mods(
        self,
        instance_id: str,
        name: str,
        snapshot: dict[str, Any],
        state: InstanceState,
        quiet: bool,
    ) -> None:
        mods = snapshot.get("mods") or {}
        updates = mods.get("updates_active") or []
        # The versions are part of the key, so news about a *different*
        # release is a different announcement rather than a repeat.
        key = ",".join(f"{u.get('package_full_name')}-{u.get('latest')}" for u in updates)
        if key != state.mod_update_key:
            state.mod_update_key = key
            if updates and not quiet:
                self._publish(
                    ev.MOD_UPDATE,
                    instance_id=instance_id,
                    instance_name=name,
                    title=(
                        f"{len(updates)} of {name}'s mods have a newer version."
                        if len(updates) > 1
                        else f"One of {name}'s mods has a newer version."
                    ),
                    detail="\n".join(
                        f"{u['name']} {u['current']} → {u['latest']}" for u in updates
                    ),
                    level="info",
                    key=f"mod-updates:{instance_id}:{key}",
                )

        pending = bool(mods.get("restart_pending"))
        if pending != state.restart_pending:
            state.restart_pending = pending
            if pending and not quiet:
                self._publish(
                    ev.MOD_RESTART_PENDING,
                    instance_id=instance_id,
                    instance_name=name,
                    title=f"{name}'s mods have changed since it started.",
                    detail=(
                        "A server loads its plugins once, when it starts, so it "
                        "is not running them yet. Restart it to pick them up."
                    ),
                    level="important",
                    key=f"mods-need-restart:{instance_id}",
                )

    def _publish(self, kind: str, **kwargs: Any) -> None:
        self._bus.publish(Event(kind=kind, **kwargs))


# --------------------------------------------------------------------------- #
# posting
# --------------------------------------------------------------------------- #
class Announcer:
    """Routes events to channels and renders them.

    Routing is the per-instance channel if the instance has one, otherwise the
    default channel; an event about no particular instance always goes to the
    default. An instance with announcements switched off is silent even if a
    default channel exists, because "off" has to mean off.
    """

    def __init__(self, manager: Any, rest: Any, config: Any) -> None:
        self.manager = manager
        self._rest = rest
        self._config = config
        #: Keys already announced, so a repeated event is dropped even if two
        #: sources report it.
        self._seen: dict[str, float] = {}
        self.sent = 0
        self.last_error = ""

    @property
    def config(self) -> DiscordConfig:
        return self._config()

    def channel_for(self, event: Event) -> str:
        config = self.config
        if not event.instance_id:
            return config.default_channel_id
        try:
            record = self.manager.get(event.instance_id)
        except Exception:  # noqa: BLE001 - a deleted instance is not an error
            return config.default_channel_id
        if not record.config.discord_enabled:
            return ""
        return record.config.discord_channel_id or config.default_channel_id

    def _already_said(self, event: Event) -> bool:
        """Whether this exact state has already been announced.

        Only events that carry a key are deduplicated: a player joining twice
        is two things happening, while "mods need a restart" is one fact
        however many times it is noticed.
        """
        if not event.key:
            return False
        now = time.time()
        # Expire old keys so a server that goes up and down all week does not
        # grow this without bound, and so the same state can be reported again
        # if it recurs much later.
        for key, at in list(self._seen.items()):
            if now - at > 86400:
                self._seen.pop(key, None)
        if event.key in self._seen:
            return True
        self._seen[event.key] = now
        return False

    async def handle(self, event: Event) -> bool:
        """Announce *event* if it should be. Returns whether anything was sent."""
        config = self.config
        if not config.announces(event.kind):
            return False
        channel = self.channel_for(event)
        if not channel:
            return False
        if self._already_said(event):
            return False

        try:
            await self._rest.create_message(channel, self.render(event))
            self.sent += 1
            self.last_error = ""
            return True
        except Exception as exc:  # noqa: BLE001 - a failed post is not fatal
            # DiscordAPIError can say what to do about it; anything else
            # only has its own text.
            explain = getattr(exc, "explain", None)
            self.last_error = explain() if callable(explain) else str(exc)
            log.warning("discord: could not announce to %s: %s", channel, self.last_error)
            return False

    def render(self, event: Event) -> dict[str, Any]:
        """One event as a Discord message payload.

        Player traffic is sent as a plain line rather than an embed: a join
        and a leave are one-line facts, and a channel of boxes for them reads
        like an alarm going off.
        """
        if event.kind in (ev.PLAYER_JOIN, ev.PLAYER_LEAVE, ev.PLAYER_DEATH):
            return {"content": event.title}

        fields: list[dict[str, Any]] = []
        snapshot = (event.data or {}).get("snapshot") or {}
        if event.kind in (ev.STATUS, ev.READY, ev.CRASH) and snapshot:
            players = snapshot.get("players") or {}
            if snapshot.get("status") == "running":
                maximum = players.get("max") or 0
                count = players.get("count") or 0
                fields.append(
                    {"name": "Players", "value": f"{count}/{maximum}" if maximum else str(count)}
                )
            if snapshot.get("version"):
                fields.append({"name": "Version", "value": snapshot["version"]})
            if self.config.show_address and event.instance_id:
                try:
                    record = self.manager.get(event.instance_id)
                    fields.append(
                        {
                            "name": "Address",
                            "value": f"`{self.manager.address_for(record)}`",
                            "inline": False,
                        }
                    )
                except Exception:  # noqa: BLE001
                    pass

        return {
            "embeds": [
                render.embed(
                    event.title,
                    description=event.detail,
                    level=event.level,
                    fields=fields,
                    footer=event.instance_name if event.instance_id else "",
                )
            ]
        }
