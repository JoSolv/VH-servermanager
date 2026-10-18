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

        self.router = cmd.CommandRouter(manager)
        self.announcer: Announcer | None = None
        #: Set when the bot could not be started at all, for the GUI to show.
        self.last_error: str = ""
        self.started_at: float = 0.0
        #: Guilds whose command set we have registered this run, so one that
        #: stops being used can have its commands taken away again.
        self._registered_guilds: set[str] = set()

    # ------------------------------------------------------------------ #
    # status, for the Settings page
    # ------------------------------------------------------------------ #
    def status(self) -> dict[str, Any]:
        gateway = self._gateway
        user = (gateway.user if gateway else {}) or {}
        return {
            "enabled": self.config.enabled,
            "configured": bool(self.guilds()),
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
            "commands_registered": bool(self._registered_guilds),
            "registered_guilds": sorted(self._registered_guilds),
            # What is set up, and where, so Settings can show the whole
            # picture without the operator opening every instance.
            "instances": self.instance_summary(),
        }

    @property
    def running(self) -> bool:
        return self._gateway_task is not None and not self._gateway_task.done()

    def guilds(self) -> set[str]:
        """Every Discord server any instance is set up to use."""
        return {
            record.config.discord.guild_id
            for record in self.manager.records
            if record.config.discord.enabled and record.config.discord.guild_id
        }

    def instance_summary(self) -> list[dict[str, Any]]:
        """One row per instance: where it posts and what it answers."""
        rows = []
        for record in self.manager.records:
            settings = record.config.discord
            rows.append({
                "id": record.config.id,
                "name": record.config.name,
                "enabled": settings.enabled,
                "guild_id": settings.guild_id,
                "channel_id": settings.channel_id,
                "commands": settings.answers_commands,
                "announcing": sum(
                    1 for kind in settings.announce if settings.announce[kind]
                ) if settings.enabled else 0,
            })
        return rows

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
        # Settings used to be manager-wide; move them onto the instances once,
        # before anything reads them, so an upgrade does not silently stop a
        # working bot.
        self.migrate_legacy_settings()

        self._rest = RestClient(token, api_base=self.settings.discord_api_base)
        self.announcer = Announcer(self.manager, self._rest)
        if not self.guilds():
            # Not an error: the bot connects anyway, so the Settings page can
            # show the invite link and the operator has somewhere to start.
            log.info("discord: no instance has a Discord server set yet")

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
        self._registered_guilds = set()
        self.started_at = 0.0

    def migrate_legacy_settings(self) -> None:
        """Move the old manager-wide settings down onto the instances, once.

        Before this, one guild, one default channel and one set of event
        switches served every instance. An upgrade must not leave a working
        bot silent, so the old values are copied onto every instance that
        does not already have its own, and ``discord.json`` is marked done.

        Instances that had announcements switched on keep them on. The rest
        get the guild and the switches but stay off, which leaves them one
        tick away from working rather than needing it all typed again.
        """
        if self.config.migrated:
            return

        legacy = config_mod.legacy_settings(self.settings.data_root)
        guild_id = legacy["guild_id"]
        if not guild_id:
            # Nothing was ever configured, so there is nothing to move and
            # the fresh defaults are already right.
            self.config.migrated = True
            config_mod.save(self.settings.data_root, self.config)
            return

        moved = 0
        for record in self.manager.records:
            settings = record.config.discord
            if settings.guild_id:
                continue                      # already has its own
            settings.guild_id = guild_id
            settings.channel_id = settings.channel_id or legacy["channel_id"]
            if legacy["announce"]:
                settings.announce = {
                    kind: bool(legacy["announce"].get(kind, settings.announce.get(kind)))
                    for kind in settings.announce
                }
            settings.show_address = legacy["show_address"]
            settings.commands_enabled = legacy["commands_enabled"]
            settings.command_channel_ids = list(legacy["command_channel_ids"])
            settings.read_role_ids = list(legacy["read_role_ids"])
            settings.control_enabled = legacy["control_enabled"]
            settings.control_role_ids = list(legacy["control_role_ids"])
            settings.control_user_ids = list(legacy["control_user_ids"])
            settings.cooldown_seconds = legacy["cooldown_seconds"]
            # An instance that was announcing with no channel of its own used
            # the manager's default; now that there is none, it needs the
            # value copied in above -- and if there was none to copy, it can
            # no longer announce, so say so rather than failing validation
            # the next time somebody saves the instance.
            if settings.enabled and settings.announces_anything and not settings.channel_id:
                settings.enabled = False
                log.warning(
                    "discord: %s was announcing with no channel of its own and "
                    "there is no default any more; switched off",
                    record.config.name,
                )
            self.manager.save(record.config.id)
            moved += 1

        self.config.migrated = True
        config_mod.save(self.settings.data_root, self.config)
        if moved:
            log.info(
                "discord: moved the manager-wide settings onto %d instance(s); "
                "each one's Discord settings are now on its own page",
                moved,
            )

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

        if config.enabled != previous.enabled or not self.running:
            await self.start()
            return

        # Commands are registered whenever settings are saved, even when
        # nothing about them changed. Registering is one idempotent PUT that
        # replaces the whole set, and the first run needs the retry: the bot
        # connects before it has been invited anywhere, so the registration
        # at READY fails with "unknown guild" -- and then the operator clicks
        # the invite link, comes back, and has nothing to press. Save is that
        # button.
        await self._register_commands()

    async def reload_token(self) -> None:
        """Reconnect after the token itself changed."""
        await self.start()

    async def refresh_commands(self) -> None:
        """Re-register the command sets after an instance's settings changed.

        Called when an instance's guild, command switch or control switch
        moves, since each guild's menu is built from the instances mapped to
        it. Safe to call when the bot is not connected: it does nothing, and
        the next connection registers from scratch.
        """
        await self._register_commands()

    # ------------------------------------------------------------------ #
    # gateway callbacks
    # ------------------------------------------------------------------ #
    async def _on_ready(self, _payload: dict[str, Any]) -> None:
        await self._register_commands()

    async def _register_commands(self) -> None:
        """Register the command set in every guild that needs one.

        A guild's command set depends on the instances mapped to it, so this
        walks them rather than registering one global set: two Discords get
        two different menus, each listing only what is usable there.

        A guild that no longer has any instance has its commands removed, so
        unmapping an instance does not leave a `/vh` behind that answers "no
        servers here" forever. That bookkeeping is only in memory, though: a
        guild unmapped while the manager was *stopped* is not known about on
        the next start, so its stale commands stay until something is mapped
        to it again. They refuse safely -- the gate is per instance, not per
        command -- so this is untidy rather than unsafe.
        """
        gateway, rest = self._gateway, self._rest
        if gateway is None or rest is None or not gateway.application_id:
            return

        wanted = cmd.command_guilds(self.manager)
        failures: list[str] = []

        for guild_id in sorted(wanted):
            try:
                await rest.register_guild_commands(
                    gateway.application_id, guild_id,
                    cmd.definitions_for_guild(self.manager, guild_id),
                )
                self._registered_guilds.add(guild_id)
                log.info("discord: commands registered in guild %s", guild_id)
            except DiscordAPIError as exc:
                self._registered_guilds.discard(guild_id)
                failures.append(f"{guild_id}: {exc.explain()}")
                log.warning(
                    "discord: could not register commands in guild %s: %s",
                    guild_id, exc,
                )

        for guild_id in sorted(self._registered_guilds - wanted):
            try:
                await rest.register_guild_commands(
                    gateway.application_id, guild_id, []
                )
                log.info("discord: commands removed from guild %s", guild_id)
            except DiscordAPIError as exc:
                log.warning(
                    "discord: could not remove commands from guild %s: %s",
                    guild_id, exc,
                )
            self._registered_guilds.discard(guild_id)

        # Registration failing does not stop announcements working, so this is
        # reported rather than fatal. The usual cause is the bot not having
        # been invited to that guild yet.
        self.last_error = (
            f"Could not register commands -- {'; '.join(failures)}" if failures else ""
        )

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
