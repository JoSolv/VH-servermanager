from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..instance import MODIFIER_KEYS, PRESETS, InstanceConfig, ValidationError
from ..mods.cache import cache_size
from ..mods.profile import ModError
from ..manager import ManagerError
from ..diagnostics import check_libraries, package_for
from ..steam import server_status
from ..util import normalise_time_of_day
from .api import config_context, config_index
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
        hostname=manager.hostname,
        build=await manager.check_update(),
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
            "mods_enabled": form.get("mods_enabled") is not None,
            "snapshot_interval": number("snapshot_interval", payload["snapshot_interval"]),
            "snapshot_keep": number("snapshot_keep", payload["snapshot_keep"]),
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
    # The mod notices need the catalogue, which is loaded in the background:
    # it is the whole Valheim community index in one response, and nobody
    # should wait for it to see this page. The notices ride the metrics
    # socket, so they turn up a tick after the load finishes.
    manager.ensure_index_background()
    manager.refresh_mod_state(instance_id)
    return _render(
        request,
        "instance.html",
        record=record,
        config=record.config,
        mods=profile.summary(),
        presets=PRESETS,
        modifier_keys=MODIFIER_KEYS,
        address=manager.address_for(record),
        hostname=manager.hostname,
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
# per-instance mod update schedule
#
# Registered above the lifecycle catch-all below, which would otherwise read
# "mod-updates" as the name of an action.
# --------------------------------------------------------------------------- #
def _mod_schedule(
    request: Request,
    instance_id: str,
    message: str = "",
    error: str = "",
    ran: bool = False,
) -> HTMLResponse:
    """Re-render the schedule panel.

    *ran* says the message above is a check's own report, which the panel
    then leaves out of its standing "last check" line rather than printing
    the same sentence twice.
    """
    manager = _manager(request)
    record = manager.get(instance_id)
    return _render(
        request,
        "partials/mod_schedule.html",
        record=record,
        config=record.config,
        message=message,
        error=error,
        ran=ran,
    )


@router.post("/instances/{instance_id}/mod-updates", response_class=HTMLResponse)
async def save_mod_schedule(
    request: Request,
    instance_id: str,
    enabled: str = Form(default=""),
    at: str = Form(default="05:00"),
    install: str = Form(default=""),
    restart: str = Form(default=""),
) -> HTMLResponse:
    """Save when this instance checks its mods, and what it does about it."""
    manager = _manager(request)
    try:
        normalised = normalise_time_of_day(at)
    except ValueError:
        return _mod_schedule(request, instance_id, error="Use a time like 05:00.")

    manager.update(
        instance_id,
        {
            "mod_update_check": bool(enabled),
            "mod_update_at": normalised,
            "mod_update_install": bool(install),
            "mod_update_restart": bool(restart),
        },
    )
    if not enabled:
        return _mod_schedule(request, instance_id, message="Mod update check off.")
    does = "installs them" if install else "reports them"
    if install and restart:
        does += " and restarts the server"
    return _mod_schedule(
        request,
        instance_id,
        message=f"Mod update check on, daily at {normalised}; it {does}.",
    )


@router.post("/instances/{instance_id}/mod-updates/run", response_class=HTMLResponse)
async def run_mod_check(request: Request, instance_id: str) -> HTMLResponse:
    """Run the scheduled check now, doing exactly what the schedule would do.

    Shielded, so the work is not abandoned half-way through installing a mod
    or restarting a server because the browser went away -- the same reason
    lifecycle actions are owned by the manager rather than by a request.
    """
    manager = _manager(request)
    try:
        run = await asyncio.shield(manager.run_mod_update(instance_id))
    except (ManagerError, ModError) as exc:
        return _mod_schedule(request, instance_id, error=str(exc))
    if run.error:
        return _mod_schedule(request, instance_id, error=run.summary(), ran=True)
    return _mod_schedule(request, instance_id, message=run.summary(), ran=True)


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
        await manager.delete(instance_id, remove_files=True)
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
        config_files=config_index(record.layout, profile.mods),
        index_count=manager.index.count,
        index_fetched_at=manager.index.fetched_at,
        index_error=index_error,
        categories=manager.index.categories(),
    )


@router.get("/instances/{instance_id}/mods/config", response_class=HTMLResponse)
async def mod_config_page(request: Request, instance_id: str, file: str = "", mod: str = ""):
    """The config editor.

    ``?mod=`` opens whatever the named package owns, which is what the Config
    button on a mod row links to: from the operator's side they are editing a
    mod's settings, and which file BepInEx happened to write is our problem.
    """
    manager = _manager(request)
    record = manager.get(instance_id)
    path = file
    if not path and mod:
        owned = config_index(record.layout, manager.profile(instance_id).mods).get(mod) or []
        path = owned[0].relative if owned else ""
    return _render(request, "mod_config.html", **config_context(request, instance_id, path=path))


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
        libraries=check_libraries(settings).to_dict(),
        package_for=package_for,
        cache_bytes=cache_size(settings.cache_dir),
        index_count=manager.index.count,
        task=manager.job,
        auto_update=manager.auto_update,
        hostname=manager.public_hostname,
        effective_hostname=manager.hostname,
        build=await manager.check_update(),
        net_available=manager.net.per_instance_available,
        net_reason=manager.net.per_instance_reason,
    )


@router.post("/settings/install", response_class=HTMLResponse)
async def install_server(
    request: Request,
    # Empty, not "1": a cleared checkbox is submitted by leaving the field
    # out, so a default of "1" made it impossible to turn off -- unchecking
    # "Restart running servers" restarted them anyway.
    restart_instances: str = Form(default=""),
):
    manager = _manager(request)
    # Background task: a full install is a multi-GB download, far longer than
    # any browser will hold the request open.
    asyncio.create_task(
        manager.run_update(validate=True, restart=bool(restart_instances))
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
):
    manager = _manager(request)
    try:
        normalised = normalise_time_of_day(at)
    except ValueError:
        # 200, not 400: htmx swaps successful responses only, so a refusal
        # sent as an error status is a form that silently does nothing.
        return HTMLResponse('<div class="alert error">Use a time like 04:00.</div>')

    manager.auto_update.enabled = bool(enabled)
    manager.auto_update.at = normalised
    manager.auto_update.restart_instances = bool(restart_instances)
    manager.auto_update.validate = True
    manager.save_state()
    state = "on" if manager.auto_update.enabled else "off"
    return HTMLResponse(
        f'<div class="alert ok">Scheduled update {state}, daily at {normalised}.</div>'
    )


@router.post("/settings/hostname", response_class=HTMLResponse)
async def save_hostname(request: Request, hostname: str = Form(default="")) -> HTMLResponse:
    """Set the address players connect to.

    Used for the address shown beside each instance and as the target of the
    reachability probe, so it wants to be what players actually type -- a
    public DNS name or IP, not the machine's internal address.
    """
    manager = _manager(request)
    cleaned = hostname.strip().strip("/")
    if cleaned:
        if "://" in cleaned or "/" in cleaned:
            return HTMLResponse(
                '<div class="alert error">Enter a hostname or IP only, '
                "without a scheme or path.</div>",
                status_code=400,
            )
        if ":" in cleaned and not cleaned.startswith("["):
            return HTMLResponse(
                '<div class="alert error">Leave the port out — each instance '
                "supplies its own.</div>",
                status_code=400,
            )
    manager.public_hostname = cleaned
    manager.save_state()
    shown = cleaned or f"{manager.hostname} (detected)"
    return HTMLResponse(f'<div class="alert ok">Server address set to {shown}.</div>')


@router.post("/settings/check-update", response_class=HTMLResponse)
async def check_update(request: Request):
    build = await _manager(request).check_update(force=True)
    return _render(request, "partials/build_status.html", build=build)
