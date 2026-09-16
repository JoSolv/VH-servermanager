"""HTML pages and the form actions behind them."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..instance import MODIFIER_KEYS, PRESETS, InstanceConfig, ValidationError
from ..mods.cache import cache_size
from ..steam import SteamError, install_steamcmd, server_status, update_server
from .templating import TEMPLATES

log = logging.getLogger("vhsm.web.routes")
router = APIRouter()


def _manager(request: Request):
    return request.app.state.manager


def _settings(request: Request):
    return request.app.state.settings


def _render(request: Request, template: str, **context: Any) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, template, context)


# --------------------------------------------------------------------------- #
# dashboard
# --------------------------------------------------------------------------- #
@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    manager = _manager(request)
    return _render(
        request,
        "index.html",
        records=manager.records,
        status=server_status(_settings(request)),
        net_available=manager.net.per_instance_available,
        net_reason=manager.net.per_instance_reason,
    )


# --------------------------------------------------------------------------- #
# create / edit
# --------------------------------------------------------------------------- #
def _config_from_form(form: dict[str, Any], base: InstanceConfig | None = None) -> InstanceConfig:
    """Build an InstanceConfig from submitted form fields."""
    payload = base.to_dict() if base else InstanceConfig().to_dict()

    def text(key: str, default: str = "") -> str:
        return str(form.get(key, default) or "").strip()

    def number(key: str, default: int) -> int:
        try:
            return int(form.get(key, default))
        except (TypeError, ValueError):
            raise ValidationError(f"{key.replace('_', ' ').title()} must be a number.")

    payload.update(
        {
            "name": text("name", payload["name"]),
            "world": text("world", payload["world"]),
            "password": text("password"),
            "port": number("port", payload["port"]),
            "public": form.get("public") is not None,
            "crossplay": form.get("crossplay") is not None,
            "preset": text("preset"),
            "save_interval": number("save_interval", payload["save_interval"]),
            "backups": number("backups", payload["backups"]),
            "backup_short": number("backup_short", payload["backup_short"]),
            "backup_long": number("backup_long", payload["backup_long"]),
            "extra_args": text("extra_args"),
            "autostart": form.get("autostart") is not None,
            "mods_enabled": form.get("mods_enabled") is not None,
        }
    )
    payload["modifiers"] = {
        key: str(form[f"modifier_{key}"])
        for key in MODIFIER_KEYS
        if form.get(f"modifier_{key}")
    }
    return InstanceConfig.from_dict(payload)


@router.get("/instances/new", response_class=HTMLResponse)
async def new_instance_form(request: Request):
    manager = _manager(request)
    used = {r.config.port for r in manager.records}
    port = 2456
    while port in used or any(abs(port - p) < 3 for p in used):
        port += 3
    draft = InstanceConfig(port=port, name="", world="Dedicated")
    return _render(
        request,
        "instance_form.html",
        config=draft,
        creating=True,
        presets=PRESETS,
        modifier_keys=MODIFIER_KEYS,
    )


@router.post("/instances")
async def create_instance(request: Request):
    form = dict(await request.form())
    config = _config_from_form(form)
    record = _manager(request).create(config)
    return RedirectResponse(f"/instances/{record.config.id}", status_code=303)


@router.get("/instances/{instance_id}", response_class=HTMLResponse)
async def instance_detail(request: Request, instance_id: str):
    manager = _manager(request)
    record = manager.get(instance_id)
    profile = manager.profile(instance_id)
    return _render(
        request,
        "instance.html",
        record=record,
        config=record.config,
        mods=profile.summary(),
        presets=PRESETS,
        modifier_keys=MODIFIER_KEYS,
    )


@router.post("/instances/{instance_id}/edit")
async def edit_instance(request: Request, instance_id: str):
    manager = _manager(request)
    form = dict(await request.form())
    record = manager.get(instance_id)
    config = _config_from_form(form, base=record.config)
    changes = config.to_dict()
    changes.pop("id", None)
    manager.update(instance_id, changes)
    return RedirectResponse(f"/instances/{instance_id}", status_code=303)


# --------------------------------------------------------------------------- #
# lifecycle actions (HTMX)
# --------------------------------------------------------------------------- #
@router.post("/instances/{instance_id}/{action}", response_class=HTMLResponse)
async def lifecycle(request: Request, instance_id: str, action: str):
    manager = _manager(request)
    record = manager.get(instance_id)

    if action == "delete":
        form = dict(await request.form())
        await manager.delete(instance_id, remove_files=form.get("remove_files") is not None)
        return HTMLResponse("", headers={"HX-Redirect": "/"})

    operations = {"start": manager.start, "stop": manager.stop, "restart": manager.restart}
    operation = operations.get(action)
    if operation is None:
        return HTMLResponse(f'<div class="alert error">Unknown action {action}.</div>', 400)

    try:
        await operation(instance_id)
    except (RuntimeError, ValidationError) as exc:
        return HTMLResponse(
            f'<div class="alert error">{exc}</div>'
            + TEMPLATES.get_template("partials/controls.html").render(record=record),
            status_code=200,
        )
    return _render(request, "partials/controls.html", record=record)


@router.get("/instances/{instance_id}/controls", response_class=HTMLResponse)
async def controls(request: Request, instance_id: str):
    return _render(request, "partials/controls.html", record=_manager(request).get(instance_id))


# --------------------------------------------------------------------------- #
# mods page
# --------------------------------------------------------------------------- #
@router.get("/instances/{instance_id}/mods", response_class=HTMLResponse)
async def mods_page(request: Request, instance_id: str):
    manager = _manager(request)
    record = manager.get(instance_id)
    profile = manager.profile(instance_id)

    index_error = ""
    try:
        await manager.index.ensure()
    except Exception as exc:  # offline is not fatal: installed mods still render
        index_error = str(exc)

    return _render(
        request,
        "mods.html",
        record=record,
        config=record.config,
        mods=profile.summary(),
        orphans=[m.package_full_name for m in profile.orphans()],
        index_count=manager.index.count,
        index_error=index_error,
        categories=manager.index.categories(),
    )


# --------------------------------------------------------------------------- #
# settings / server installation
# --------------------------------------------------------------------------- #
class SetupTask:
    """A single long-running steamcmd job, with output the page can poll."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.running = False
        self.task: asyncio.Task | None = None

    def log(self, message: str) -> None:
        self.lines.append(message)
        del self.lines[:-400]

    async def run(self, coro_factory) -> None:
        if self.running:
            self.log("[manager] another setup job is already running")
            return
        self.running = True
        self.lines.clear()
        try:
            await coro_factory()
        except SteamError as exc:
            self.log(f"[error] {exc}")
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            self.log(f"[error] unexpected: {exc}")
            log.exception("setup job failed")
        finally:
            self.running = False
            self.log("[manager] done")


def _setup_task(request: Request) -> SetupTask:
    task = getattr(request.app.state, "setup_task", None)
    if task is None:
        task = SetupTask()
        request.app.state.setup_task = task
    return task


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    settings = _settings(request)
    return _render(
        request,
        "settings.html",
        status=server_status(settings),
        settings=settings,
        cache_bytes=cache_size(settings.cache_dir),
        index_count=_manager(request).index.count,
        task=_setup_task(request),
        net_available=_manager(request).net.per_instance_available,
        net_reason=_manager(request).net.per_instance_reason,
    )


@router.post("/settings/install", response_class=HTMLResponse)
async def install_server(request: Request, validate_files: str = Form(default="")):
    settings = _settings(request)
    task = _setup_task(request)

    async def job() -> None:
        await install_steamcmd(settings, task.log)
        task.log("[manager] running steamcmd app_update 896660")
        async for line in update_server(settings, validate=bool(validate_files)):
            task.log(line)

    asyncio.create_task(task.run(job))
    await asyncio.sleep(0.2)
    return _render(request, "partials/setup_log.html", task=task)


@router.get("/settings/log", response_class=HTMLResponse)
async def setup_log(request: Request):
    return _render(request, "partials/setup_log.html", task=_setup_task(request))
