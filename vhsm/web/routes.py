from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..discord import config as discord_config
from ..discord.rest import DiscordAPIError
from ..events import KINDS
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


def _discord(request: Request):
    return request.app.state.discord


#: How each event kind is described on the Settings page. Written as what the
#: operator would hear in the channel rather than as the constant's name.
ANNOUNCE_TEXT: dict[str, str] = {
    "status": "Server starting, running or stopped",
    "ready": "Server up and accepting players",
    "crash": "Server stopped unexpectedly",
    "player_join": "A player joined",
    "player_leave": "A player left",
    "player_death": "A player died",
    "notice": "A recognised failure in the console",
    "mod_update": "A mod has a newer version",
    "mod_restart_pending": "Mods changed, so a restart is needed",
    "mod_check": "What the scheduled mod check found",
    "server_update": "A newer Valheim build was published",
    "server_updated": "The server files were updated",
    "reachability": "Reachable or unreachable from outside",
    "snapshot": "An automatic world snapshot was taken",
    "manager": "The manager started or stopped",
    "lifecycle": "Somebody asked for a start, stop or restart",
}


def announce_labels() -> list[tuple[str, str]]:
    """Every event kind with its label, in the order events.KINDS lists them.

    Driven off ``KINDS`` rather than off the dictionary above so that a new
    event kind appears on the page as soon as it exists -- with its bare name
    if nobody has written a label yet, which is ugly and visible rather than
    missing and silent. The test suite checks that none of them is bare.
    """
    return [(kind, ANNOUNCE_TEXT.get(kind, kind)) for kind in KINDS]


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
        discord=_discord(request).status(),
        discord_default=_discord(request).config.default_channel_id,
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
# per-instance Discord routing
#
# Registered above the lifecycle catch-all below, which would otherwise read
# "discord" as the name of an action.
# --------------------------------------------------------------------------- #
def _instance_discord(
    request: Request, instance_id: str, message: str = "", error: str = ""
) -> HTMLResponse:
    record = _manager(request).get(instance_id)
    bot = _discord(request)
    return _render(
        request,
        "partials/instance_discord.html",
        record=record,
        config=record.config,
        discord=bot.status(),
        discord_default=bot.config.default_channel_id,
        message=message,
        error=error,
    )


@router.post("/instances/{instance_id}/discord", response_class=HTMLResponse)
async def save_instance_discord(
    request: Request,
    instance_id: str,
    enabled: str = Form(default=""),
    channel_id: str = Form(default=""),
) -> HTMLResponse:
    """Choose whether, and where, this instance announces.

    Takes effect on the next event with no reconnection: the channel is read
    per announcement rather than held by the connection.
    """
    manager = _manager(request)
    try:
        manager.update(
            instance_id,
            {
                "discord_enabled": bool(enabled),
                "discord_channel_id": str(channel_id or "").strip(),
            },
        )
    except ValidationError as exc:
        return _instance_discord(request, instance_id, error=str(exc))

    record = manager.get(instance_id)
    if not record.config.discord_enabled:
        return _instance_discord(
            request, instance_id, message="This server will not announce to Discord."
        )
    where = (
        f"channel {record.config.discord_channel_id}"
        if record.config.discord_channel_id else "the default channel"
    )
    return _instance_discord(
        request, instance_id, message=f"Announcing to {where}."
    )


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
        **_discord_context(request),
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


# --------------------------------------------------------------------------- #
# Discord bot
# --------------------------------------------------------------------------- #
def _discord_context(request: Request) -> dict[str, Any]:
    bot = _discord(request)
    return {
        "discord": bot.status(),
        "config": bot.config,
        "announce_labels": announce_labels(),
    }


def _discord_panel(request: Request, message: str = "", error: str = "") -> HTMLResponse:
    """Re-render the whole panel, so every endpoint reports in one place."""
    return _render(
        request, "partials/discord.html",
        message=message, error=error, **_discord_context(request),
    )


@router.get("/settings/discord", response_class=HTMLResponse)
async def discord_panel(request: Request) -> HTMLResponse:
    return _discord_panel(request)


@router.post("/settings/discord", response_class=HTMLResponse)
async def save_discord(request: Request) -> HTMLResponse:
    """Save the bot's configuration, reconnecting only if it has to."""
    bot = _discord(request)
    form = await request.form()
    config = discord_config.DiscordConfig.from_dict(bot.config.to_dict())

    config.enabled = form.get("enabled") is not None
    config.commands_enabled = form.get("commands_enabled") is not None
    config.show_address = form.get("show_address") is not None
    config.guild_id = str(form.get("guild_id") or "")
    config.default_channel_id = str(form.get("default_channel_id") or "")
    config.command_channel_ids = str(form.get("command_channel_ids") or "")
    config.read_role_ids = str(form.get("read_role_ids") or "")
    # An unchecked box submits nothing, so the switched-on kinds are exactly
    # the ones present -- rather than the ones whose value happens to be "1",
    # which is how a cleared checkbox stays set.
    chosen = set(form.getlist("announce"))
    config.announce = {kind: kind in chosen for kind in KINDS}

    try:
        config.validate()
    except discord_config.DiscordConfigError as exc:
        # 200, not 400: htmx swaps successful responses only, so a refusal
        # sent as an error status is a form that silently does nothing.
        return _discord_panel(request, error=str(exc))

    await bot.reload(config)
    if not config.enabled:
        return _discord_panel(request, message="Discord bot off.")
    status = bot.status()
    if status["last_error"]:
        return _discord_panel(request, error=status["last_error"])
    return _discord_panel(request, message="Discord settings saved.")


@router.post("/settings/discord/token", response_class=HTMLResponse)
async def save_discord_token(
    request: Request,
    token: str = Form(default=""),
    clear: str = Form(default=""),
) -> HTMLResponse:
    """Store or remove the bot token.

    The field is write-only: it is never rendered back, so an empty submission
    means "leave it alone" rather than "clear it". Clearing is its own button.
    """
    bot = _discord(request)
    root = _settings(request).data_root
    if clear:
        discord_config.clear_token(root)
        await bot.reload_token()
        return _discord_panel(request, message="Bot token removed.")
    if not token.strip():
        return _discord_panel(request, error="Paste a token, or use Clear token.")
    try:
        discord_config.write_token(root, token)
    except discord_config.DiscordConfigError as exc:
        return _discord_panel(request, error=str(exc))
    await bot.reload_token()
    status = bot.status()
    if status["last_error"]:
        return _discord_panel(request, error=status["last_error"])
    if not status["enabled"]:
        return _discord_panel(
            request, message="Token saved. Switch the bot on to connect."
        )
    return _discord_panel(request, message="Token saved.")


@router.post("/settings/discord/test", response_class=HTMLResponse)
async def test_discord(request: Request) -> HTMLResponse:
    """Post one message, so "is it actually wired up" has a real answer."""
    bot = _discord(request)
    channel = bot.config.default_channel_id
    if not channel:
        return _discord_panel(request, error="Set a default channel first.")
    try:
        await bot.say(
            channel,
            "Valheim Server Manager is connected and will announce here.",
        )
    except DiscordAPIError as exc:
        return _discord_panel(request, error=exc.explain())
    return _discord_panel(request, message=f"Test message sent to {channel}.")


@router.post("/settings/check-update", response_class=HTMLResponse)
async def check_update(request: Request):
    build = await _manager(request).check_update(force=True)
    return _render(request, "partials/build_status.html", build=build)
