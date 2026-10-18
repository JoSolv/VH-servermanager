"""The manager's event bus: things that happened, for anything that cares.

The sampling :class:`~vhsm.manager.Hub` already broadcasts *state* -- a full
snapshot of every instance, twice a second, for a browser to render. That is
the wrong shape for a notifier, which needs to know what *changed* and needs
to be told once. Diffing the snapshot stream covers some of it, but not the
things a snapshot cannot express: which mod was installed, who asked for a
restart, what a scheduled check actually did.

So events are published here, by the code that knows, and subscribers read
them from a queue. Nothing in this module knows about Discord; the bot is one
subscriber, and a webhook or an email sink later would be another.

A subscriber that stops reading is dropped rather than allowed to block the
manager: the queue is bounded and a full one loses its oldest event. Missing a
notification is a disappointment, stalling the sampling loop is an outage.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("vhsm.events")

#: Events held for a subscriber that is not keeping up. Generous, because the
#: only subscriber is a bot that may be mid-reconnect: a server restart with
#: twenty players on it is a few dozen events in a couple of seconds, and
#: those are exactly the ones worth keeping.
QUEUE_LIMIT = 256


# --------------------------------------------------------------------------- #
# event kinds
#
# Strings rather than an enum, because they are also the keys of the operator's
# per-event "announce this" toggles and go to disk in discord.json. An enum
# would have to be converted at both ends for no gain.
# --------------------------------------------------------------------------- #
#: An instance's process changed state: starting, running, stopped, crashed.
STATUS = "status"
#: A started server is actually up -- holding its port, version known.
READY = "ready"
#: The process exited without being asked to.
CRASH = "crash"
#: A player connected, and we now know their character name.
PLAYER_JOIN = "player_join"
#: A player disconnected.
PLAYER_LEAVE = "player_leave"
#: A player died.
PLAYER_DEATH = "player_death"
#: A recognised failure was seen in an instance's console output.
NOTICE = "notice"
#: An enabled mod has a newer version on Thunderstore.
MOD_UPDATE = "mod_update"
#: The mods on disk no longer match what the running server loaded.
MOD_RESTART_PENDING = "mod_restart_pending"
#: A scheduled or manual mod check finished.
MOD_CHECK = "mod_check"
#: A newer dedicated-server build was published.
SERVER_UPDATE = "server_update"
#: The dedicated server was updated, or the attempt failed.
SERVER_UPDATED = "server_updated"
#: An instance stopped answering its query socket from outside, or resumed.
REACHABILITY = "reachability"
#: An automatic world snapshot was taken.
SNAPSHOT = "snapshot"
#: The manager itself started or stopped.
MANAGER = "manager"
#: A lifecycle action was asked for, and by whom. Published for the audit
#: trail -- the outcome arrives separately as STATUS.
LIFECYCLE = "lifecycle"

#: Every kind, in the order the settings page lists them.
KINDS: tuple[str, ...] = (
    STATUS, READY, CRASH,
    PLAYER_JOIN, PLAYER_LEAVE, PLAYER_DEATH,
    NOTICE, MOD_UPDATE, MOD_RESTART_PENDING, MOD_CHECK,
    SERVER_UPDATE, SERVER_UPDATED, REACHABILITY, SNAPSHOT,
    MANAGER, LIFECYCLE,
)

#: Which kinds are announced unless the operator says otherwise. The noisy
#: ones are off: a busy server is a join and a leave for every player every
#: evening, and whether that is news or spam is a matter of taste rather than
#: something to guess. Deaths are off for the same reason, and because on a
#: hardcore server they are somebody's bad evening rather than a status update.
DEFAULT_ANNOUNCE: dict[str, bool] = {
    STATUS: True,
    READY: True,
    CRASH: True,
    PLAYER_JOIN: False,
    PLAYER_LEAVE: False,
    PLAYER_DEATH: False,
    NOTICE: True,
    MOD_UPDATE: True,
    MOD_RESTART_PENDING: True,
    MOD_CHECK: True,
    SERVER_UPDATE: True,
    SERVER_UPDATED: True,
    REACHABILITY: True,
    SNAPSHOT: False,
    MANAGER: False,
    LIFECYCLE: False,
}


@dataclass(slots=True)
class Event:
    """One thing that happened.

    *instance_id* is empty for events about the manager or the shared server
    install, which is what decides whether an announcement is routed to one
    instance's channel or to the default one.
    """

    kind: str
    instance_id: str = ""
    instance_name: str = ""
    #: One line, already in the operator's terms. Subscribers that render
    #: text use this rather than rebuilding a sentence from the fields.
    title: str = ""
    #: Optional second line: the detail behind the title.
    detail: str = ""
    #: ``info`` | ``good`` | ``important`` | ``bad``, mapped to a colour by
    #: whatever renders it.
    level: str = "info"
    #: Stable identity for the *state* this event reports, where it reports
    #: one. Two events with the same key say the same thing, which is what
    #: lets a notifier skip the second. Blank for events that are inherently
    #: one-off, like a player joining.
    key: str = ""
    #: Anything a subscriber might want that is not in the sentence.
    data: dict[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "instance_id": self.instance_id,
            "instance_name": self.instance_name,
            "title": self.title,
            "detail": self.detail,
            "level": self.level,
            "key": self.key,
            "data": self.data,
            "at": self.at,
        }


class EventBus:
    """Fan-out of :class:`Event` to subscribers.

    Shaped like the websocket :class:`~vhsm.manager.Hub` on purpose -- same
    subscribe/unsubscribe/publish -- so there is one pattern in the codebase
    for "several things want to hear about this" rather than two.
    """

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()

    def subscribe(self) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(queue)

    @property
    def listeners(self) -> int:
        return len(self._subscribers)

    def publish(self, event: Event) -> None:
        """Hand *event* to every subscriber. Never raises, never blocks.

        Called from the sampling loop and from lifecycle code, so a subscriber
        cannot be allowed to make either of them wait. A full queue drops its
        oldest event: the newest state is the one worth having, and a notifier
        that fell behind would otherwise report a server as starting long
        after it had started.
        """
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
            except Exception:  # noqa: BLE001 - publishing must never propagate
                log.debug("event subscriber rejected %s", event.kind, exc_info=True)
