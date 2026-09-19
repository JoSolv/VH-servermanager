"""Lifecycle of a single dedicated-server process.

One :class:`Supervisor` owns one OS process: it launches it, streams its
output to subscribers and to disk, and shuts it down gracefully. It knows
nothing about HTTP or about the other instances.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import os
import re
import signal
import time
from collections import deque
from pathlib import Path
from typing import Callable, TextIO

from .config import Settings, VALHEIM_CLIENT_APPID
from .instance import InstanceConfig, InstanceLayout
from .diagnostics import check_playfab_egress
from .logspam import CROSSPLAY_PLAYFAB_UNREACHABLE, RE_JOIN_CODE, IssueWatcher, LogThrottle
from .mods import bepinex

#: Lines of console output kept in memory per instance.
LOG_BUFFER_LINES = 1000
#: How long to wait for a graceful save-and-exit before escalating signals.
SIGINT_GRACE = 45.0
SIGTERM_GRACE = 15.0

#: The server announces its build on the first line of output, e.g.
#: ``Valheim version: l-0.217.46``. This is the version a client must match.
RE_VERSION = re.compile(r"Valheim version:?\s*(?:l-)?([0-9][0-9.]*)", re.IGNORECASE)


class Status(str, enum.Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    CRASHED = "crashed"

    @property
    def is_active(self) -> bool:
        return self in (Status.STARTING, Status.RUNNING, Status.STOPPING)


LogHook = Callable[[str], None]


class Supervisor:
    """Owns the process for one instance."""

    def __init__(
        self,
        config: InstanceConfig,
        layout: InstanceLayout,
        settings: Settings,
    ) -> None:
        self.config = config
        self.layout = layout
        self.settings = settings

        self.status: Status = Status.STOPPED
        self.pid: int | None = None
        self.started_at: float | None = None
        self.exit_code: int | None = None
        self.last_error: str | None = None
        #: Build reported by the running server, from its own console output.
        self.server_version: str = ""
        #: Join code issued once a crossplay server's PlayFab Party network is
        #: up. It is how console and Game Pass players reach the server -- the
        #: address is no use to them -- and it is reissued on every boot, so it
        #: belongs on screen rather than buried in a console that has scrolled.
        self.join_code: str = ""

        self._process: asyncio.subprocess.Process | None = None
        self._pump: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._buffer: deque[str] = deque(maxlen=LOG_BUFFER_LINES)
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._log_hooks: list[LogHook] = []
        #: A server stuck in a retry loop writes the same lines thousands of
        #: times a second. Left alone that fills the disk, floods every open
        #: browser, and pushes the reason it got stuck out of the buffer.
        self._throttle = LogThrottle()
        self._issues = IssueWatcher()

    # ------------------------------------------------------------------ #
    # logging
    # ------------------------------------------------------------------ #
    def add_log_hook(self, hook: LogHook) -> None:
        """Register a callback invoked for every console line (e.g. player tracking)."""
        self._log_hooks.append(hook)

    def recent_logs(self, limit: int = LOG_BUFFER_LINES) -> list[str]:
        return list(self._buffer)[-limit:]

    def subscribe(self) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=500)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        self._subscribers.discard(queue)

    @property
    def notices(self) -> list[dict[str, object]]:
        """Known failures recognised in this run, explained in plain words."""
        return self._issues.to_list()

    def _emit(self, line: str, record: bool = True) -> None:
        """Account for one line, and show it unless it is being throttled.

        *record* only decides whether the line reaches the transcript and the
        live console. Everything that reads the stream for meaning -- the
        version banner, the player tracker -- is fed either way, so collapsing
        a flood costs nothing but the repeated text.
        """
        match = RE_VERSION.search(line)
        if match:
            self.server_version = match.group(1)
        code = RE_JOIN_CODE.search(line)
        if code:
            self.join_code = code.group(1)
        for hook in self._log_hooks:
            try:
                hook(line)
            except Exception:  # a broken hook must never kill the log pump
                pass
        if not record:
            return
        stamped = f"{time.strftime('%H:%M:%S')} {line}"
        self._buffer.append(stamped)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(stamped)
            except asyncio.QueueFull:
                # Slow consumer: drop the line rather than stall the server.
                pass

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def _working_dir(self) -> Path:
        """Directory to launch from.

        The stock ``start_server.sh`` cds into the install directory, and the
        Steam game-server API resolves ``steamclient.so`` and writes its
        bookkeeping relative to the working directory. Launching from
        somewhere else can leave Steam half-initialised -- the game still
        accepts direct connections, but the query port never answers, so the
        server shows as unreachable in the client's browser. Instance state is
        kept separate through absolute ``-savedir`` and Doorstop paths, so
        sharing this directory between instances is safe.
        """
        game_dir = self.settings.game_dir
        return game_dir if game_dir.is_dir() else self.layout.root

    def _build_command(self) -> list[str]:
        binary = self.settings.server_binary
        args = self.config.launch_args(self.layout)
        if self.settings.fake_server:
            return ["python3", str(binary), *args]
        return [str(binary), *args]

    def _build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["SteamAppId"] = VALHEIM_CLIENT_APPID
        # The server writes crash dumps and Steam files relative to HOME, so
        # it needs one it can write. Assigned rather than defaulted: HOME is
        # always already set, so setdefault would never take effect.
        env["HOME"] = str(self.layout.root)
        lib_paths = [str(self.settings.game_dir / "linux64"), str(self.settings.game_dir)]
        if env.get("LD_LIBRARY_PATH"):
            lib_paths.append(env["LD_LIBRARY_PATH"])
        env["LD_LIBRARY_PATH"] = ":".join(lib_paths)

        if self.config.mods_enabled and bepinex.is_installed(self.layout):
            env.update(bepinex.launch_env(self.layout, self.settings.game_dir))
        return env

    def _say(self, line: str, sink: TextIO | None = None) -> None:
        """Record a manager line in the transcript and in the live console.

        The pump normally holds the transcript open and passes its handle. A
        line emitted before it starts -- a preflight warning, say -- appends to
        the file itself, because a line that only ever reaches the in-memory
        buffer is missing from the very download someone reaches for when a
        server has misbehaved.
        """
        try:
            if sink is not None:
                sink.write(line + "\n")
                sink.flush()
            else:
                self.layout.logs.mkdir(parents=True, exist_ok=True)
                with self.layout.console_log.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except OSError as exc:  # a full or read-only disk must not stop a start
            self._emit(f"[manager] could not write the console log: {exc}")
        self._emit(line)

    async def _preflight(self) -> None:
        """Say up front what this host will make the server do wrong.

        Only crossplay gets checked, because it is the only mode that depends
        on somewhere other than this machine answering. The check is the one
        that would have caught the failure this project has actually hit: a
        filter on the network path swallowing the requests the server makes on
        its way up, leaving it retrying forever with nothing in the log that
        says why.

        Run off the event loop -- a filtered network does not refuse a
        connection, it sits on it until the timeout.
        """
        if not self.config.crossplay:
            return
        egress = await asyncio.to_thread(check_playfab_egress)
        if egress.ok:
            return
        notice = self._issues.raise_now(CROSSPLAY_PLAYFAB_UNREACHABLE)
        if notice is None:
            return
        self._say(f"[manager] {egress.detail}")
        for text in notice.console_lines():
            self._say(text)

    async def start(self) -> None:
        async with self._lock:
            if self.status.is_active:
                raise RuntimeError(f"Instance {self.config.name!r} is already {self.status.value}.")

            binary = self.settings.server_binary
            if not binary.exists():
                raise RuntimeError(
                    f"Server binary missing at {binary}. Install the dedicated "
                    "server from the Settings page first."
                )

            self.layout.ensure()
            self.config.validate()

            self.status = Status.STARTING
            self.exit_code = None
            self.last_error = None
            # Reissued on every boot, so last run's code is worse than none.
            self.join_code = ""
            # Both describe this run of this process, not the instance, so a
            # restart starts them over rather than carrying the last run's
            # complaints forward.
            self._throttle.reset()
            self._issues.clear()
            self._say(f"[manager] starting {self.config.name!r} on port {self.config.port}")
            await self._preflight()

            try:
                self._process = await asyncio.create_subprocess_exec(
                    *self._build_command(),
                    cwd=str(self._working_dir()),
                    env=self._build_env(),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    stdin=asyncio.subprocess.DEVNULL,
                    # Own session so we can signal the whole process group and
                    # so Ctrl-C on the manager does not reach the servers.
                    start_new_session=True,
                )
            except OSError as exc:
                self.status = Status.CRASHED
                self.last_error = str(exc)
                self._say(f"[manager] launch failed: {exc}")
                raise RuntimeError(f"Failed to launch: {exc}") from exc

            self.pid = self._process.pid
            self.started_at = time.time()
            self.status = Status.RUNNING
            self._pump = asyncio.create_task(self._pump_output())

    async def _pump_output(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        log_file = self.layout.console_log
        log_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            with log_file.open("a", encoding="utf-8", errors="replace") as sink:
                async for raw in self._process.stdout:
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    # A line the throttle holds back is still accounted for:
                    # _emit feeds the hooks either way, so only the text is
                    # dropped, never the meaning.
                    verdict = self._throttle.admit(line)
                    if verdict.note:
                        self._say(verdict.note, sink)
                    if verdict.keep:
                        self._say(line, sink)
                    else:
                        self._emit(line, record=False)
                    notice = self._issues.observe(line)
                    if notice is not None:
                        for text in notice.console_lines():
                            self._say(text, sink)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._emit(f"[manager] log pump error: {exc}")
        finally:
            await self._reap()

    async def _reap(self) -> None:
        if self._process is None:
            return
        self.exit_code = await self._process.wait()
        was_stopping = self.status is Status.STOPPING
        self.status = Status.STOPPED if was_stopping or self.exit_code == 0 else Status.CRASHED
        if self.status is Status.CRASHED:
            self.last_error = f"Process exited with code {self.exit_code}."
        self._emit(f"[manager] process exited with code {self.exit_code}")
        # The PlayFab session died with the process, so the code it was issued
        # is no longer one anybody can join with. Keeping it on screen would
        # only get it handed out.
        self.join_code = ""
        self.pid = None
        self.started_at = None
        self._process = None

    async def stop(self, timeout: float = SIGINT_GRACE) -> None:
        """Ask the server to save and exit, escalating if it refuses."""
        async with self._lock:
            process = self._process
            if process is None or process.returncode is not None:
                self.status = Status.STOPPED
                return

            self.status = Status.STOPPING
            self._emit("[manager] stopping (SIGINT, waiting for world save)")

            # SIGINT is Valheim's documented graceful shutdown: it saves the
            # world before exiting. Escalate only if it does not comply.
            for sig, grace in ((signal.SIGINT, timeout), (signal.SIGTERM, SIGTERM_GRACE)):
                self._signal_group(sig)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=grace)
                    return
                self._emit(f"[manager] still alive after {sig.name}, escalating")

            self._signal_group(signal.SIGKILL)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=10)

    def _signal_group(self, sig: signal.Signals) -> None:
        if self.pid is None:
            return
        try:
            os.killpg(os.getpgid(self.pid), sig)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            self._emit(f"[manager] cannot signal process group: {exc}")

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    @property
    def uptime(self) -> float:
        return time.time() - self.started_at if self.started_at else 0.0
