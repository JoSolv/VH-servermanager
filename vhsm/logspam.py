"""Keeping a runaway server from drowning its own console.

A dedicated server stuck in a retry loop emits the same handful of lines
thousands of times a second, and nothing downstream is built for that: the
transcript on disk grows without bound, every open browser is flooded over
the websocket, and whatever the server said *before* it got stuck scrolls out
of the in-memory tail within a second. The loop is the least useful thing in
the log and it is the only thing left in it.

:class:`LogThrottle` bounds that. It groups lines by shape rather than by
exact text -- timestamps and ids differ between copies of the same message --
and once one shape arrives faster than a person could read it, further copies
are left out and replaced by a periodic count. Leaving a line out is
cosmetic: callers hand every line to their log hooks either way, so player
tracking and version detection still see the whole stream.

:class:`IssueWatcher` reads the same stream for failures that are known,
survivable, and impossible to diagnose from the log itself -- above all the
crossplay public-IP loop, which is a bug in the game binary rather than
anything the manager can fix -- and turns them into one plain explanation.
"""

from __future__ import annotations

import re
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Sequence

#: Digits carry the timestamps, ids, ports and counters that differ between
#: otherwise identical messages, so they are exactly what a signature drops.
RE_HEX = re.compile(r"0x[0-9a-fA-F]+")
RE_DIGITS = re.compile(r"\d+")
RE_SPACE = re.compile(r"\s+")

#: Signatures are truncated so one runaway line with a long, varying tail
#: cannot masquerade as thousands of distinct shapes.
SIGNATURE_LENGTH = 160


def signature(line: str) -> str:
    """The *shape* of a line: what two copies of one message have in common."""
    text = RE_HEX.sub("0x#", line)
    text = RE_DIGITS.sub("#", text)
    return RE_SPACE.sub(" ", text).strip()[:SIGNATURE_LENGTH]


def excerpt(line: str, limit: int = 80) -> str:
    """A one-line, length-bounded quote of *line* for a manager message."""
    text = RE_SPACE.sub(" ", line).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass(slots=True)
class Verdict:
    """What to do with one console line."""

    #: Whether the line belongs in the transcript and the live console.
    keep: bool
    #: A manager line to record first, or "" when there is nothing to say.
    note: str = ""


@dataclass(slots=True)
class _Run:
    """How often one signature has turned up lately, and what was dropped."""

    hits: deque[float]
    #: Copies left out since the last count was reported.
    suppressed: int = 0
    #: Copies left out since suppression started, for the closing message.
    total: int = 0
    suppressing: bool = False
    last_note: float = 0.0


class LogThrottle:
    """Rate-limits repeated console lines, by shape, over a sliding window.

    The defaults let anything a healthy server says through untouched: thirty
    copies of one shape inside twenty seconds means roughly two a second,
    sustained, which no normal server output reaches. A retry loop clears it
    in a few milliseconds.
    """

    def __init__(
        self,
        burst: int = 30,
        window: float = 20.0,
        note_interval: float = 30.0,
        track: int = 512,
    ) -> None:
        self.burst = burst
        self.window = window
        self.note_interval = note_interval
        self.track = track
        self._runs: OrderedDict[str, _Run] = OrderedDict()

    def reset(self) -> None:
        """Forget every signature. Called between runs of a server."""
        self._runs.clear()

    def admit(self, line: str, now: float | None = None) -> Verdict:
        """Decide whether *line* is recorded, and what to say about it."""
        now = time.monotonic() if now is None else now
        key = signature(line)

        run = self._runs.get(key)
        if run is None:
            # maxlen caps the memory one shape can hold; the window trim
            # below is what actually decides whether it is repeating.
            run = _Run(hits=deque(maxlen=self.burst + 1))
            self._runs[key] = run
            while len(self._runs) > self.track:
                self._runs.popitem(last=False)
        else:
            self._runs.move_to_end(key)

        run.hits.append(now)
        while run.hits and now - run.hits[0] > self.window:
            run.hits.popleft()

        if len(run.hits) <= self.burst:
            if not run.suppressing:
                return Verdict(True)
            dropped = run.total
            run.suppressing = False
            run.suppressed = run.total = 0
            return Verdict(
                True,
                f"[manager] that line stopped repeating; {dropped} "
                f"{'copy was' if dropped == 1 else 'copies were'} left out of the console",
            )

        run.suppressed += 1
        run.total += 1
        if not run.suppressing:
            run.suppressing = True
            run.last_note = now
            return Verdict(
                False,
                f'[manager] "{excerpt(line)}" is repeating faster than it can be '
                "read; further copies are left out of the console until it stops "
                "(the manager still reads every one of them)",
            )
        if now - run.last_note >= self.note_interval:
            run.last_note = now
            dropped, run.suppressed = run.suppressed, 0
            return Verdict(
                False,
                f"[manager] still repeating: {dropped} more copies left out in "
                f"the last {int(self.note_interval)}s",
            )
        return Verdict(False)


# --------------------------------------------------------------------------- #
# known failures worth explaining
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class KnownIssue:
    """A failure the console cannot explain on its own."""

    key: str
    pattern: re.Pattern[str]
    #: Matches needed before it is reported. One is noise; a loop is not.
    threshold: int
    title: str
    detail: str


@dataclass(slots=True)
class Notice:
    """A raised :class:`KnownIssue`, with how much of it has been seen."""

    key: str
    title: str
    detail: str
    hits: int = 0
    raised_at: float = field(default_factory=time.time)

    def console_lines(self) -> list[str]:
        """The notice as manager lines, for the log the user is staring at."""
        return [f"[manager] {self.title}"] + [
            f"[manager] {part.strip()}"
            for part in self.detail.splitlines()
            if part.strip()
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "detail": self.detail,
            "hits": self.hits,
            "raised_at": self.raised_at,
        }


CROSSPLAY_PUBLIC_IP_LOOP = KnownIssue(
    key="crossplay-public-ip-loop",
    # Every turn of the loop ends with this line, whichever lookup service it
    # tried, so counting it counts the loop itself.
    pattern=re.compile(
        r"could not extract valid ip address from externalip", re.IGNORECASE
    ),
    #: A handful of these at boot is ordinary; a loop passes this in a blink.
    threshold=15,
    title=(
        "This server cannot look up its public IPv6 address and is retrying "
        "it in a tight loop."
    ),
    detail=(
        "A crossplay server asks an outside service for its public IPv6 address "
        "on the way to the PlayFab relay. Only the first of those requests can "
        "ever reach the network: the game reuses one HttpClient and sets a "
        "timeout on it before each request, which .NET refuses once that client "
        "has sent anything, so every retry throws InvalidOperationException "
        "immediately instead of waiting. On a host with a routable IPv6 address "
        "the first lookup succeeds and the matter ends there; on a host without "
        "one it fails, and the retries then spin as fast as the CPU allows.\n"
        "This is in the game binary and nothing here can patch it. The repeats "
        "are left out of the console so the rest of the log stays "
        "readable, and the server itself carries on -- this is a background "
        "lookup, not the game loop -- though a server spinning like this can be "
        "slow to shut down.\n"
        "To end it, give this host a routable IPv6 address: with Docker's "
        "default bridge network the container has none, so either use host "
        "networking on an IPv6-capable host or turn IPv6 on for the network. "
        "Turning crossplay off also ends it, since crossplay is what wants the "
        "address, at the cost of console and Game Pass players."
    ),
)

#: Every issue the watcher knows how to recognise.
ISSUES: tuple[KnownIssue, ...] = (CROSSPLAY_PUBLIC_IP_LOOP,)


class IssueWatcher:
    """Counts known failure patterns and raises one notice per failure."""

    def __init__(self, issues: Sequence[KnownIssue] = ISSUES) -> None:
        self._issues = tuple(issues)
        self._counts: dict[str, int] = {}
        self._notices: dict[str, Notice] = {}

    def observe(self, line: str) -> Notice | None:
        """Feed one console line in. Returns a notice only when it is new."""
        for issue in self._issues:
            if not issue.pattern.search(line):
                continue
            count = self._counts.get(issue.key, 0) + 1
            self._counts[issue.key] = count
            raised = self._notices.get(issue.key)
            if raised is not None:
                raised.hits = count
                return None
            if count >= issue.threshold:
                return self._raise(issue, count)
        return None

    def raise_now(self, issue: KnownIssue) -> Notice | None:
        """Raise *issue* from evidence outside the log, e.g. a preflight check.

        Returns None when it is already raised, so a caller can print the
        notice exactly once without tracking that itself.
        """
        if issue.key in self._notices:
            return None
        return self._raise(issue, self._counts.get(issue.key, 0))

    def _raise(self, issue: KnownIssue, hits: int) -> Notice:
        notice = Notice(issue.key, issue.title, issue.detail, hits=hits)
        self._notices[issue.key] = notice
        return notice

    def clear(self) -> None:
        """Forget everything. Called when a server is started afresh."""
        self._counts.clear()
        self._notices.clear()

    @property
    def notices(self) -> list[Notice]:
        return list(self._notices.values())

    def to_list(self) -> list[dict[str, Any]]:
        return [notice.to_dict() for notice in self._notices.values()]
