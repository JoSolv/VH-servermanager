"""Application factory and lifespan wiring."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config import Settings, settings as default_settings
from ..instance import ValidationError
from ..manager import InstanceManager, ManagerError

log = logging.getLogger("vhsm.web")

HERE = Path(__file__).resolve().parent


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or default_settings
    manager = InstanceManager(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        manager.load_all()
        manager.start_background()
        if not manager.net.per_instance_available:
            log.warning(
                "per-instance network accounting unavailable: %s",
                manager.net.per_instance_reason,
            )
        await manager.start_autostart()
        try:
            yield
        finally:
            await manager.stop_background()
            await manager.shutdown_all()

    app = FastAPI(title="Valheim Server Manager", lifespan=lifespan, version="0.1.0")
    app.state.manager = manager
    app.state.settings = settings
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
