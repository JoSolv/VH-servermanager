"""HTML pages and the form actions behind them."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..instance import MODIFIER_KEYS, PRESETS, InstanceConfig, ValidationError
from ..mods.cache import cache_size
from ..manager import ManagerError
from ..steam import server_status
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
    """Kick off a lifecycle action and return at once.

    The work runs as a manager-owned task rather than inside this request:
    stopping a real server takes as long as saving the world does, and the
    dashboard swaps these very buttons when the status changes, which aborts
    the in-flight request. Awaiting here meant a cancelled request could stop
    a server and never start it again.
    """
    manager = _manager(request)
    record = manager.get(instance_id)

    if action == "delete":
        form = dict(await request.form())
        await manager.delete(instance_id, remove_files=form.get("remove_files") is not None)
        return HTMLResponse("", headers={"HX-Redirect": "/"})

    try:
        manager.submit(instance_id, action)
    except (ManagerError, RuntimeError, ValidationError) as exc:
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
@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    settings = _settings(request)
    manager = _manager(request)
    return _render(
        request,
        "settings.html",
        status=server_status(settings),
        settings=settings,
        cache_bytes=cache_size(settings.cache_dir),
        index_count=manager.index.count,
        task=manager.job,
        auto_update=manager.auto_update,
        build=await manager.check_update(),
        net_available=manager.net.per_instance_available,
        net_reason=manager.net.per_instance_reason,
    )


@router.post("/settings/install", response_class=HTMLResponse)
async def install_server(
    request: Request,
    validate_files: str = Form(default=""),
    restart_instances: str = Form(default="1"),
):
    manager = _manager(request)
    # Background task: a full install is a multi-GB download, far longer than
    # any browser will hold the request open.
    asyncio.create_task(
        manager.run_update(validate=bool(validate_files), restart=bool(restart_instances))
    )
    await asyncio.sleep(0.3)
    return _render(request, "partials/setup_log.html", task=manager.job)


@router.get("/settings/log", response_class=HTMLResponse)
async def setup_log(request: Request):
    return _render(request, "partials/setup_log.html", task=_manager(request).job)


@router.post("/settings/auto-update", response_class=HTMLResponse)
async def save_auto_update(
    request: Request,
    enabled: str = Form(default=""),
    at: str = Form(default="04:00"),
    restart_instances: str = Form(default=""),
    validate_files: str = Form(default=""),
):
    manager = _manager(request)
    try:
        hour, _, minute = at.partition(":")
        if not (0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
            raise ValueError
        normalised = f"{int(hour):02d}:{int(minute):02d}"
    except ValueError:
        return HTMLResponse('<div class="alert error">Use a time like 04:00.</div>', 400)

    manager.auto_update.enabled = bool(enabled)
    manager.auto_update.at = normalised
    manager.auto_update.restart_instances = bool(restart_instances)
    manager.auto_update.validate = bool(validate_files)
    manager.save_state()
    state = "on" if manager.auto_update.enabled else "off"
    return HTMLResponse(
        f'<div class="alert ok">Scheduled update {state}, daily at {normalised}.</div>'
    )


@router.post("/settings/check-update", response_class=HTMLResponse)
async def check_update(request: Request):
    build = await _manager(request).check_update(force=True)
    return _render(request, "partials/build_status.html", build=build)
