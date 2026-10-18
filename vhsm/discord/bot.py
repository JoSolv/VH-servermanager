"""The bot itself: one connection, the command router, the announcer.

Owned by the web application's lifespan alongside the manager's background
loops. Everything here is built so that Discord being unreachable, misconfigured
or rejecting the token is a line in Settings and nothing more: the manager
must keep running servers whatever Discord thinks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from ..events import Event
from . import commands as cmd
from . import config as config_mod
from .announce import Announcer, StateWatcher
from .config import DiscordConfig
from .gateway import GatewayClient
from .rest import DiscordAPIError, RestClient

log = logging.getLogger("vhsm.discord")


class DiscordBot:
    """The optional Discord integration, in one object.

    Start it with :meth:`start` and stop it with :meth:`stop`; both are safe
    to call whatever state it is in, which is what lets the Settings page
    reconfigure it without restarting the manager.
    """

    def __init__(self, manager: Any) -> None:
        self.manager = manager
        self.settings = manager.settings
        self.config: DiscordConfig = config_mod.load(self.settings.data_root)

        self._rest: RestClient | None = None
        self._gateway: GatewayClient | None = None
        self._gateway_task: asyncio.Task[None] | None = None
        self._pump_task: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[Event] | None = None

        self.router = cmd.CommandRouter(manager, lambda: self.config)
        self.announcer: Announcer | None = None
        #: Set when the bot could not be started at all, for the GUI to show.
        self.last_error: str = ""
        self.started_at: float = 0.0
        self._commands_registered = False

    # ------------------------------------------------------------------ #
    # status, for the Settings page
    # ------------------------------------------------------------------ #
    def status(self) -> dict[str, Any]:
        gateway = self._gateway
        user = (gateway.user if gateway else {}) or {}
        return {
            "enabled": self.config.enabled,
            "configured": self.config.configured,
            "token_source": config_mod.token_source(self.settings.data_root),
            "state": gateway.state if gateway else "stopped",
            "connected": bool(gateway and gateway.connected),
            "username": user.get("username") or "",
            "discriminator": user.get("discriminator") or "",
            "application_id": gateway.application_id if gateway else "",
            "invite_url": (
                config_mod.invite_url(gateway.application_id)
                if gateway and gateway.application_id else ""
            ),
            "connects": gateway.connects if gateway else 0,
            "connected_at": gateway.connected_at if gateway else 0.0,
            "started_at": self.started_at,
            "announcements_sent": self.announcer.sent if self.announcer else 0,
            # The gateway's error, the announcer's, or our own -- whichever
            # is the reason nothing is happening.
            "last_error": (
                self.last_error
                or (gateway.last_error if gateway else "")
                or (self.announcer.last_error if self.announcer else "")
            ),
            "commands_registered": self._commands_registered,
        }

    @property
    def running(self) -> bool:
        return self._gateway_task is not None and not self._gateway_task.done()

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """Connect, if the operator has switched this on and configured it.

        Returns quietly when it is off or incomplete: "not configured" is the
        normal state of an optional feature, not a failure to report.
        """
        await self.stop()
        self.last_error = ""
        if not self.config.enabled:
            return
        token = config_mod.read_token(self.settings.data_root)
        if not token:
            self.last_error = "No bot token is set."
            return
        if not self.config.guild_id:
            self.last_error = "No Discord server (guild) ID is set."
            return

        self._rest = RestClient(
            token, api_base=self.settings.discord_api_base
        )
        self.announcer = Announcer(self.manager, self._rest, lambda: self.config)

        try:
            url = (
                self.settings.discord_gateway_url
                or await self._rest.gateway_url()
            )
        except DiscordAPIError as exc:
            # The commonest failure by far, and the one worth explaining: a
            # token that was pasted wrong, or revoked.
            self.last_error = exc.explain()
            log.error("discord: %s", self.last_error)
            await self._close_rest()
            return

        self._gateway = GatewayClient(
            token,
            gateway_url=url,
            on_ready=self._on_ready,
            on_dispatch=self._on_dispatch,
        )
        self._queue = self.manager.events.subscribe()
        self._pump_task = asyncio.create_task(self._pump())
        self._gateway_task = asyncio.create_task(self._gateway.run())
        self.started_at = time.time()
        log.info("discord bot started")

    async def stop(self) -> None:
        if self._gateway is not None:
            await self._gateway.stop()
        for attr in ("_gateway_task", "_pump_task"):
            task = getattr(self, attr)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
                setattr(self, attr, None)
        if self._queue is not None:
            self.manager.events.unsubscribe(self._queue)
            self._queue = None
        await self._close_rest()
        self._gateway = None
        self.announcer = None
        self._commands_registered = False
        self.started_at = 0.0

    async def _close_rest(self) -> None:
        if self._rest is not None:
            with contextlib.suppress(Exception):
                await self._rest.close()
            self._rest = None

    async def reload(self, config: DiscordConfig) -> None:
        """Adopt a new configuration, reconnecting only if it has to.

        Channel and announcement changes take effect on the next event with
        no reconnection at all; the token, the guild and the on/off switch are
        what the connection is built from, so those restart it.
        """
        previous = self.config
        self.config = config
        config_mod.save(self.settings.data_root, config)

        needs_restart = (
            config.enabled != previous.enabled
            or config.guild_id != previous.guild_id
            or not self.running
        )
        if needs_restart:
            await self.start()
        elif config.control_enabled != previous.control_enabled:
            # The registered command set depends on this, since a command
            # nobody may run is not offered at all.
            await self._register_commands()

    async def reload_token(self) -> None:
        """Reconnect after the token itself changed."""
        await self.start()

    # ------------------------------------------------------------------ #
    # gateway callbacks
    # ------------------------------------------------------------------ #
    async def _on_ready(self, _payload: dict[str, Any]) -> None:
        await self._register_commands()

    async def _register_commands(self) -> None:
        gateway, rest = self._gateway, self._rest
        if gateway is None or rest is None or not gateway.application_id:
            return
        if not self.config.guild_id:
            return
        try:
            await rest.register_guild_commands(
                gateway.application_id,
                self.config.guild_id,
                cmd.definitions(self.config),
            )
            self._commands_registered = True
            log.info("discord: commands registered in guild %s", self.config.guild_id)
        except DiscordAPIError as exc:
            self._commands_registered = False
            # Registration failing does not stop announcements working, so
            # this is reported rather than fatal. The usual cause is the bot
            # not having been invited to the guild yet.
            self.last_error = (
                f"Could not register commands: {exc.explain()}"
            )
            log.warning("discord: %s", self.last_error)

    async def _on_dispatch(self, name: str, data: dict[str, Any]) -> None:
        if name != "INTERACTION_CREATE":
            return
        await self.handle_interaction(data)

    async def handle_interaction(self, interaction: dict[str, Any]) -> None:
        """Answer one interaction, within the three seconds Discord allows.

        A command marked ``defer`` is acknowledged first and filled in after,
        so a handler that has to ask Steam something does not show the user
        "the application did not respond".
        """
        rest = self._rest
        if rest is None:
            return
        kind = interaction.get("type")
        interaction_id = str(interaction.get("id") or "")
        token = str(interaction.get("token") or "")
        if not interaction_id or not token:
            return

        if kind == cmd.INTERACTION_AUTOCOMPLETE:
            with contextlib.suppress(DiscordAPIError):
                await rest.respond_to_interaction(
                    interaction_id, token, self.router.autocomplete(interaction)
                )
            return

        if kind != cmd.INTERACTION_COMMAND:
            return

        name, _, _ = cmd.parse_command(interaction)
        command = cmd.COMMANDS.get(name)

        if command is not None and command.defer:
            try:
                await rest.respond_to_interaction(
                    interaction_id, token, {"type": cmd.RESPONSE_DEFERRED}
                )
            except DiscordAPIError as exc:
                log.warning("discord: could not acknowledge interaction: %s", exc)
                return
            response = await self.router.dispatch(interaction)
            application_id = self._gateway.application_id if self._gateway else ""
            if response and application_id:
                with contextlib.suppress(DiscordAPIError):
                    await rest.edit_interaction_response(
                        application_id, token, response.get("data") or {}
                    )
            return

        response = await self.router.dispatch(interaction)
        if response is None:
            return
        try:
            await rest.respond_to_interaction(interaction_id, token, response)
        except DiscordAPIError as exc:
            log.warning("discord: could not answer interaction: %s", exc)

    # ------------------------------------------------------------------ #
    # announcements
    # ------------------------------------------------------------------ #
    async def _pump(self) -> None:
        """Read the event bus and announce what should be announced.

        Runs whether or not the gateway is connected: posting is REST, so an
        announcement goes out even while the websocket is reconnecting.
        """
        queue = self._queue
        assert queue is not None
        while True:
            event = await queue.get()
            if self.announcer is None:
                continue
            try:
                await self.announcer.handle(event)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad event must not end the pump
                log.exception("discord: could not announce %s", event.kind)

    async def say(self, channel_id: str, content: str) -> None:
        """Post one plain message. Used by the Settings page's test button."""
        if self._rest is None:
            raise DiscordAPIError(0, "The bot is not connected.")
        await self._rest.create_message(channel_id, {"content": content})

    async def announce_now(self, event: Event) -> bool:
        """Announce *event* without going through the queue.

        For the one case where the queue is no use: the manager shutting down.
        Publishing it on the bus and then cancelling the pump in the same
        breath is a message that never gets sent, so this awaits the post.
        """
        if self.announcer is None:
            return False
        try:
            return await self.announcer.handle(event)
        except Exception:  # noqa: BLE001 - shutting down regardless
            log.debug("discord: could not announce %s", event.kind, exc_info=True)
            return False


__all__ = ["DiscordBot", "StateWatcher"]
