"""Application factory and lifespan wiring."""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config import Settings, settings as default_settings
from ..discord.announce import StateWatcher
from ..discord.bot import DiscordBot
from .. import events as ev
from ..events import Event
from ..instance import ValidationError
from ..manager import InstanceManager, ManagerError
from .templating import TEMPLATES

log = logging.getLogger("vhsm.web")

HERE = Path(__file__).resolve().parent


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or default_settings
    manager = InstanceManager(settings)
    # Announcements are derived from the samples the manager already takes, so
    # the watcher is attached whether or not the bot is switched on: the cost
    # is a dictionary comparison per instance per tick, and having it always
    # on means switching the bot on in Settings needs no restart.
    manager.attach_watcher(StateWatcher(manager.events))
    bot = DiscordBot(manager)
    # The development banner sits in the base layout, so every page needs the
    # flag. Passing it per route meant the two routes that did were the only
    # ones that rendered at all -- the rest raised UndefinedError on it.
    TEMPLATES.env.globals["vhsm_dev_mode"] = settings.fake_server

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        manager.load_all()
        manager.start_background()
        if not manager.net.per_instance_available:
            log.warning(
                "per-instance network accounting unavailable: %s",
                manager.net.per_instance_reason,
            )
        # A Discord problem is never allowed to stop the manager from booting:
        # the bot reports its own trouble on the Settings page, and servers
        # are managed either way.
        try:
            await bot.start()
            manager.events.publish(Event(
                kind=ev.MANAGER,
                title="The Valheim server manager is up.",
                detail=f"Managing {len(manager.records)} server(s).",
                level="good",
                key=f"manager-up:{time.time():.0f}",
            ))
        except Exception:  # noqa: BLE001
            log.exception("could not start the Discord bot")
        try:
            yield
        finally:
            # Awaited rather than published: the queue is about to be
            # cancelled, so a message put on it now would never be sent.
            await bot.announce_now(Event(
                kind=ev.MANAGER,
                title="The Valheim server manager is shutting down.",
                detail="Running servers are being stopped.",
                level="important",
                key=f"manager-down:{time.time():.0f}",
            ))
            await bot.stop()
            await manager.stop_background()
            await manager.shutdown_all()

    app = FastAPI(title="Valheim Server Manager", lifespan=lifespan, version="0.1.0")
    app.state.manager = manager
    app.state.settings = settings
    app.state.discord = bot
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    from . import api, routes, ws

    app.include_router(routes.router)
    app.include_router(api.router)
    app.include_router(ws.router)

    @app.exception_handler(ValidationError)
    async def _validation_handler(request: Request, exc: ValidationError):
        return _error_response(request, str(exc), 400)

    @app.exception_handler(ManagerError)
    async def _manager_handler(request: Request, exc: ManagerError):
        return _error_response(request, str(exc), 404)

    return app


def _error_response(request: Request, message: str, status: int):
    """HTMX requests get an inline banner; everything else gets JSON."""
    if request.headers.get("HX-Request"):
        return HTMLResponse(
            f'<div class="alert error" role="alert">{message}</div>', status_code=status
        )
    return JSONResponse({"error": message}, status_code=status)


app = create_app()
