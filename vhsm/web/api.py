"""JSON API plus the HTMX endpoints that drive the mod manager.

Mod mutations return the re-rendered mod list, because that is what the page
needs; read-only endpoints under ``/api`` return JSON so the manager can also
be scripted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from starlette.background import BackgroundTask

from ..instance import InstanceConfig, InstanceLayout, ValidationError
from ..manager import ManagerError
from ..playerlists import KICK_BAN_SECONDS, PlayerListError
from ..archive import ArchiveError, SUFFIX, extract_zip
from ..backups import BackupError
from ..worlds import WorldError
from ..mods.cache import cache_size, clear_cache
from ..mods import config as modconfig
from ..mods.config import ConfigError
from ..mods.profile import InstalledMod, ModError
from ..mods.thunderstore import ThunderstoreError
from ..monitor.metrics import host_metrics
from ..steam import server_status
from .templating import TEMPLATES

log = logging.getLogger("vhsm.web.api")
router = APIRouter()

MAX_UPLOAD_BYTES = 64 * 1024 * 1024
#: Worlds can be large, so instance archives get their own, higher ceiling.
MAX_ARCHIVE_UPLOAD = 4 * 1024 * 1024 * 1024


def _manager(request: Request):
    return request.app.state.manager


def _mods_partial(request: Request, instance_id: str, message: str = "", error: str = "") -> HTMLResponse:
    manager = _manager(request)
    record = manager.get(instance_id)
    profile = manager.profile(instance_id)
    return TEMPLATES.TemplateResponse(
        request,
        "partials/installed_mods.html",
        {
            "record": record,
            "mods": profile.summary(),
            "orphans": [m.package_full_name for m in profile.orphans()],
            "config_files": config_index(record.layout, profile.mods),
            "message": message,
            "error": error,
        },
    )


def config_index(
    layout: InstanceLayout, mods: list[InstalledMod]
) -> dict[str, list[modconfig.ConfigFile]]:
    """Config files per owning mod, for the Config button on each row.

    Scanning re-reads the config tree, which is a handful of small text files;
    doing it on every render keeps the button honest about whether the mod has
    anything to configure yet.
    """
    return modconfig.index_by_owner(modconfig.scan(layout, mods))


def config_context(
    request: Request,
    instance_id: str,
    *,
    path: str = "",
    mode: str = "form",
    message: str = "",
    error: str = "",
) -> dict[str, Any]:
    """Everything the config editor renders: the file list and the open file.

    Returned as a context rather than a response so the full page and the
    HTMX partial can both build from it.
    """
    manager = _manager(request)
    record = manager.get(instance_id)
    mods = manager.profile(instance_id).mods

    files = modconfig.scan(record.layout, mods)
    groups = modconfig.group_by_mod(files, mods)

    selected = next((f for f in files if f.relative == path), None)
    document = None
    if selected is not None:
        try:
            document = modconfig.read_document(selected.path)
        except ConfigError as exc:
            error = error or str(exc)
    elif path and not error:
        error = f"{path} is no longer there."

    # A file with nothing parseable in it has nothing to build a form from,
    # so it opens in the raw editor whatever the caller asked for.
    if document is not None and not document.structured:
        mode = "raw"

    return {
        "record": record,
        "config": record.config,
        "groups": groups,
        "file_count": len(files),
        "selected": selected,
        "document": document,
        "mode": "raw" if mode == "raw" else "form",
        "running": record.supervisor.status.value in ("starting", "running"),
        "message": message,
        "error": error,
    }


def _config_workspace(request: Request, instance_id: str, **kwargs: Any) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request, "partials/mod_config.html", config_context(request, instance_id, **kwargs)
    )


# --------------------------------------------------------------------------- #
# read-only JSON
# --------------------------------------------------------------------------- #
@router.get("/api/host")
async def api_host(request: Request) -> JSONResponse:
    manager = _manager(request)
    return JSONResponse(
        {
            "host": host_metrics(),
            "host_net": manager.net.host().to_dict(),
            "server": server_status(request.app.state.settings),
            "cache_bytes": cache_size(request.app.state.settings.cache_dir),
            "net_per_instance": {
                "available": manager.net.per_instance_available,
                "reason": manager.net.per_instance_reason,
            },
        }
    )


@router.get("/api/instances")
async def api_instances(request: Request) -> JSONResponse:
    return JSONResponse([r.snapshot() for r in _manager(request).records])


@router.get("/api/instances/{instance_id}")
async def api_instance(request: Request, instance_id: str) -> JSONResponse:
    manager = _manager(request)
    record = manager.get(instance_id)
    payload = record.snapshot()
    payload["config"] = record.config.to_dict()
    payload["mods"] = manager.profile(instance_id).summary()
    return JSONResponse(payload)


@router.post("/api/instances")
async def api_create_instance(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(400, f"invalid JSON body: {exc}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "expected a JSON object")
    payload.pop("id", None)
    record = _manager(request).create(InstanceConfig.from_dict(payload))
    return JSONResponse(record.snapshot(), status_code=201)


@router.get("/api/instances/{instance_id}/logs")
async def api_logs(request: Request, instance_id: str, limit: int = 200) -> JSONResponse:
    record = _manager(request).get(instance_id)
    return JSONResponse({"lines": record.supervisor.recent_logs(max(1, min(limit, 1000)))})


# --------------------------------------------------------------------------- #
# Thunderstore browsing
# --------------------------------------------------------------------------- #
@router.get("/api/mods/search", response_class=HTMLResponse)
async def mods_search(
    request: Request,
    instance_id: str = "",
    q: str = "",
    category: str = "",
    offset: int = 0,
    limit: int = 24,
    include_deprecated: str = "",
) -> HTMLResponse:
    manager = _manager(request)
    limit = max(1, min(limit, 100))
    try:
        await manager.index.ensure()
    except ThunderstoreError as exc:
        # Render the error *inside* the results container: swapping the
        # container away would leave later searches without a target.
        return TEMPLATES.TemplateResponse(
            request,
            "partials/search_results.html",
            {
                "packages": [], "total": 0, "offset": 0, "limit": limit,
                "query": q, "category": category, "instance_id": instance_id,
                "installed": {}, "error": str(exc),
            },
        )

    packages, total = manager.index.search(
        q,
        limit=limit,
        offset=max(0, offset),
        category=category,
        include_deprecated=bool(include_deprecated),
    )

    installed: dict[str, str] = {}
    if instance_id:
        try:
            installed = {
                m.package_full_name.lower(): m.version
                for m in manager.profile(instance_id).mods
            }
        except Exception:  # noqa: BLE001 - browsing without an instance is fine
            installed = {}

    return TEMPLATES.TemplateResponse(
        request,
        "partials/search_results.html",
        {
            "packages": [p.to_dict() for p in packages],
            "total": total,
            "offset": offset,
            "limit": limit,
            "query": q,
            "category": category,
            "instance_id": instance_id,
            "installed": installed,
        },
    )


@router.post("/api/mods/refresh", response_class=HTMLResponse)
async def refresh_index(request: Request, instance_id: str = Form(default="")) -> HTMLResponse:
    manager = _manager(request)
    try:
        await manager.index.ensure(force=True)
    except ThunderstoreError as exc:
        return HTMLResponse(f'<div class="alert error">{exc}</div>')  # targets #refresh-note
    return HTMLResponse(
        f'<div class="alert ok">Catalogue refreshed: {manager.index.count} packages.</div>'
    )


@router.get("/api/mods/{package_full_name}/versions")
async def package_versions(request: Request, package_full_name: str) -> JSONResponse:
    manager = _manager(request)
    await manager.index.ensure()
    package = manager.index.get(package_full_name)
    if package is None:
        raise HTTPException(404, f"{package_full_name} not found")
    return JSONResponse(package.to_dict(with_versions=True))


# --------------------------------------------------------------------------- #
# mod mutations (HTMX)
# --------------------------------------------------------------------------- #
@router.post("/api/instances/{instance_id}/mods/install", response_class=HTMLResponse)
async def install_mod(
    request: Request,
    instance_id: str,
    package_full_name: str = Form(...),
    version: str = Form(default=""),
) -> HTMLResponse:
    manager = _manager(request)
    profile = manager.profile(instance_id)
    notes: list[str] = []
    try:
        await manager.index.ensure()
        installed = await profile.install_by_name(
            package_full_name, version, progress=notes.append
        )
    except (ModError, ThunderstoreError) as exc:
        return _mods_partial(request, instance_id, error=str(exc))

    # Installing the first mod is a clear signal that the server should boot
    # with BepInEx; flip the flag rather than making the user find it.
    if installed and not manager.get(instance_id).config.mods_enabled:
        manager.update(instance_id, {"mods_enabled": True})

    names = ", ".join(m.full_name for m in installed) or "nothing new"
    return _mods_partial(request, instance_id, message=f"Installed {names}.")


@router.post("/api/instances/{instance_id}/mods/{package_full_name}/toggle", response_class=HTMLResponse)
async def toggle_mod(request: Request, instance_id: str, package_full_name: str) -> HTMLResponse:
    profile = _manager(request).profile(instance_id)
    mod = profile.get(package_full_name)
    if mod is None:
        return _mods_partial(request, instance_id, error=f"{package_full_name} is not installed.")
    try:
        updated = profile.set_enabled(package_full_name, not mod.enabled)
    except ModError as exc:
        return _mods_partial(request, instance_id, error=str(exc))
    state = "enabled" if updated.enabled else "disabled"
    return _mods_partial(request, instance_id, message=f"{updated.name} {state}.")


@router.post("/api/instances/{instance_id}/mods/{package_full_name}/uninstall", response_class=HTMLResponse)
async def uninstall_mod(request: Request, instance_id: str, package_full_name: str) -> HTMLResponse:
    profile = _manager(request).profile(instance_id)
    dependents = profile.dependents_of(package_full_name)
    if dependents:
        names = ", ".join(m.name for m in dependents)
        return _mods_partial(
            request,
            instance_id,
            error=f"{package_full_name} is required by {names}. Remove those first.",
        )
    try:
        mod = profile.uninstall(package_full_name)
    except ModError as exc:
        return _mods_partial(request, instance_id, error=str(exc))
    return _mods_partial(request, instance_id, message=f"Removed {mod.full_name}.")


@router.post("/api/instances/{instance_id}/mods/update-all", response_class=HTMLResponse)
async def update_all_mods(request: Request, instance_id: str) -> HTMLResponse:
    manager = _manager(request)
    profile = manager.profile(instance_id)
    try:
        await manager.index.ensure()
        pending = profile.updates_available()
        for entry in pending:
            await profile.install_by_name(entry["package_full_name"], entry["latest"])
    except (ModError, ThunderstoreError) as exc:
        return _mods_partial(request, instance_id, error=str(exc))
    if not pending:
        return _mods_partial(request, instance_id, message="Everything is already up to date.")
    return _mods_partial(request, instance_id, message=f"Updated {len(pending)} mod(s).")


@router.post("/api/instances/{instance_id}/mods/prune", response_class=HTMLResponse)
async def prune_orphans(request: Request, instance_id: str) -> HTMLResponse:
    profile = _manager(request).profile(instance_id)
    removed = []
    for mod in profile.orphans():
        try:
            profile.uninstall(mod.package_full_name)
            removed.append(mod.name)
        except ModError:
            continue
    message = f"Removed unused dependencies: {', '.join(removed)}." if removed else "No unused dependencies."
    return _mods_partial(request, instance_id, message=message)


# --------------------------------------------------------------------------- #
# per-mod configuration
# --------------------------------------------------------------------------- #
def _submitted_values(form: Any, document: "modconfig.ConfigDocument") -> dict[tuple[str, str], str]:
    """Read the config form back.

    Each rendered entry carries its section and key in hidden fields, so a
    value is matched to the entry it belongs to rather than to whatever now
    sits at that position -- the server may have rewritten the file while the
    form was open. How to read the field is decided from the entry on disk,
    never from the submission, so a crafted form cannot widen what it may set.
    """
    values: dict[tuple[str, str], str] = {}
    for name in set(form.keys()):
        if not (name.startswith("s") and name[1:].isdigit()):
            continue
        index = name[1:]
        section = str(form.get(f"s{index}") or "")
        key = str(form.get(f"n{index}") or "")
        entry = document.get(section, key) if key else None
        if entry is None:
            continue
        submitted = [str(v) for v in form.getlist(f"v{index}")]
        if entry.kind == "flags":
            # A flags entry is a group of checkboxes: none ticked is a real
            # answer (the empty set), not a missing field.
            values[(section, key)] = ", ".join(submitted)
        elif submitted:
            # Booleans render a hidden "false" ahead of the checkbox, so the
            # last value submitted is the one the operator actually chose.
            values[(section, key)] = submitted[-1]
    return values


@router.get("/api/instances/{instance_id}/mods/config/view", response_class=HTMLResponse)
async def config_view(
    request: Request, instance_id: str, path: str = "", mode: str = "form"
) -> HTMLResponse:
    """The config editor: file list on the left, the open file on the right."""
    return _config_workspace(request, instance_id, path=path, mode=mode)


@router.post("/api/instances/{instance_id}/mods/config/save", response_class=HTMLResponse)
async def config_save(request: Request, instance_id: str) -> HTMLResponse:
    form = await request.form()
    path = str(form.get("path") or "")
    mode = "raw" if str(form.get("mode") or "") == "raw" else "form"
    record = _manager(request).get(instance_id)

    try:
        target = modconfig.resolve_config_path(record.layout, path)
        document = modconfig.read_document(target)
    except ConfigError as exc:
        return _config_workspace(request, instance_id, path=path, mode=mode, error=str(exc))

    if mode == "raw":
        text = str(form.get("text") or "")
        if len(text.encode("utf-8")) > modconfig.MAX_EDITABLE_BYTES:
            return _config_workspace(
                request, instance_id, path=path, mode=mode, error="That is too much text."
            )
        modconfig.write_text(target, modconfig.normalise_submitted(text, document))
        return _config_workspace(
            request, instance_id, path=path, mode=mode, message=f"Saved {target.name}."
        )

    changed, problems = document.apply(_submitted_values(form, document))
    if changed:
        modconfig.write_text(target, document.text)

    message = f"Saved {changed} setting(s) in {target.name}." if changed else ""
    if not changed and not problems:
        message = "Nothing changed."
    error = " ".join(problems)
    if problems:
        error = f"Kept the previous value for {len(problems)} setting(s): {error}"
    return _config_workspace(
        request, instance_id, path=path, mode=mode, message=message, error=error
    )


@router.post("/api/instances/{instance_id}/mods/config/reset", response_class=HTMLResponse)
async def config_reset(request: Request, instance_id: str) -> HTMLResponse:
    """Put one setting, or a whole file, back to the values the mod shipped."""
    form = await request.form()
    path = str(form.get("path") or "")
    section = str(form.get("section") or "")
    key = str(form.get("key") or "")
    record = _manager(request).get(instance_id)

    try:
        target = modconfig.resolve_config_path(record.layout, path)
        document = modconfig.read_document(target)
    except ConfigError as exc:
        return _config_workspace(request, instance_id, path=path, error=str(exc))

    if key:
        entry = document.get(section, key)
        if entry is None:
            return _config_workspace(
                request, instance_id, path=path, error=f"{key} is no longer in this file."
            )
        if not entry.has_default:
            return _config_workspace(
                request, instance_id, path=path, error=f"{key} does not record a default."
            )
        try:
            changed = document.set_value(entry, entry.default)
        except ConfigError as exc:
            return _config_workspace(request, instance_id, path=path, error=str(exc))
        message = f"{key} reset to {entry.default or 'empty'}." if changed else f"{key} was already default."
    else:
        changed = bool(document.reset_to_defaults())
        message = (
            f"{target.name} reset to defaults."
            if changed
            else f"{target.name} was already all defaults."
        )

    if changed:
        modconfig.write_text(target, document.text)
    return _config_workspace(request, instance_id, path=path, message=message)


@router.post("/api/instances/{instance_id}/mods/config/delete", response_class=HTMLResponse)
async def config_delete(request: Request, instance_id: str) -> HTMLResponse:
    """Delete a config file so the mod writes a fresh one at the next boot."""
    form = await request.form()
    path = str(form.get("path") or "")
    record = _manager(request).get(instance_id)
    try:
        target = modconfig.resolve_config_path(record.layout, path)
    except ConfigError as exc:
        return _config_workspace(request, instance_id, error=str(exc))
    try:
        target.unlink(missing_ok=True)
    except OSError as exc:
        return _config_workspace(request, instance_id, path=path, error=str(exc))
    return _config_workspace(
        request,
        instance_id,
        message=f"Deleted {target.name}. The mod writes a fresh one the next time the server starts.",
    )


@router.get("/api/instances/{instance_id}/mods/config/download")
async def config_download(request: Request, instance_id: str, path: str = ""):
    record = _manager(request).get(instance_id)
    try:
        target = modconfig.resolve_config_path(record.layout, path)
    except ConfigError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not target.is_file():
        raise HTTPException(404, f"{path} does not exist")
    return FileResponse(target, filename=target.name, media_type="text/plain")


@router.get("/api/instances/{instance_id}/mods/config")
async def api_config(request: Request, instance_id: str, path: str = "") -> JSONResponse:
    """The config catalogue as JSON, or one parsed file when *path* is given."""
    manager = _manager(request)
    record = manager.get(instance_id)
    mods = manager.profile(instance_id).mods
    if not path:
        return JSONResponse(modconfig.summary(record.layout, mods))
    try:
        target = modconfig.resolve_config_path(record.layout, path)
        document = modconfig.read_document(target)
    except ConfigError as exc:
        raise HTTPException(400, str(exc)) from exc
    payload = document.to_dict()
    payload["path"] = target.relative_to(modconfig.config_root(record.layout)).as_posix()
    return JSONResponse(payload)


# --------------------------------------------------------------------------- #
# profile export / import
# --------------------------------------------------------------------------- #
@router.get("/api/instances/{instance_id}/mods/export")
async def export_profile(request: Request, instance_id: str) -> JSONResponse:
    manager = _manager(request)
    record = manager.get(instance_id)
    payload = manager.profile(instance_id).export(record.config.name)
    filename = f"{record.config.slug}-modprofile.json"
    return JSONResponse(
        payload,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/api/instances/{instance_id}/mods/import", response_class=HTMLResponse)
async def import_profile(
    request: Request, instance_id: str, file: UploadFile = File(...)
) -> HTMLResponse:
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        return _mods_partial(request, instance_id, error="That file is too large.")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _mods_partial(request, instance_id, error=f"Not a valid profile export: {exc}")

    manager = _manager(request)
    profile = manager.profile(instance_id)
    notes: list[str] = []
    try:
        await manager.index.ensure()
        installed = await profile.import_mods(payload, progress=notes.append)
    except (ModError, ThunderstoreError) as exc:
        return _mods_partial(request, instance_id, error=str(exc))
    skipped = [n for n in notes if n.startswith("skipped")]
    message = f"Imported {len(installed)} mod(s)."
    if skipped:
        message += " " + "; ".join(skipped)
    return _mods_partial(request, instance_id, message=message)


@router.post("/api/instances/{instance_id}/mods/upload", response_class=HTMLResponse)
async def upload_mod(
    request: Request, instance_id: str, file: UploadFile = File(...)
) -> HTMLResponse:
    """Install a Thunderstore-style zip that is not on Thunderstore."""
    from ..mods.cache import store_upload
    from ..mods.thunderstore import PackageVersion

    if not (file.filename or "").lower().endswith(".zip"):
        return _mods_partial(request, instance_id, error="Please upload a .zip package.")
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        return _mods_partial(request, instance_id, error="That file is too large.")

    manager = _manager(request)
    settings = request.app.state.settings
    stem = Path(file.filename).stem
    namespace, name, version = "local", stem, "0.0.0"
    parts = stem.split("-")
    if len(parts) >= 3:
        namespace, name, version = parts[0], parts[1], parts[2]
    elif len(parts) == 2:
        namespace, name = parts

    package = PackageVersion(
        full_name=f"{namespace}-{name}-{version}",
        name=name,
        namespace=namespace,
        version_number=version,
        description="Manually uploaded package",
    )

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as handle:
        handle.write(raw)
        temp_path = Path(handle.name)
    try:
        store_upload(settings.cache_dir, package, temp_path)
        profile = manager.profile(instance_id)
        await profile.install(package)
    except (ModError, ThunderstoreError, RuntimeError) as exc:
        return _mods_partial(request, instance_id, error=str(exc))
    finally:
        temp_path.unlink(missing_ok=True)
    return _mods_partial(request, instance_id, message=f"Installed {package.full_name} from upload.")


@router.post("/api/cache/clear")
async def clear_package_cache(request: Request) -> JSONResponse:
    settings = request.app.state.settings
    before = cache_size(settings.cache_dir)
    clear_cache(settings.cache_dir)
    return JSONResponse({"freed": before})


# --------------------------------------------------------------------------- #
# admin / ban / permit lists
# --------------------------------------------------------------------------- #
def _players_partial(
    request: Request, instance_id: str, message: str = "", error: str = ""
) -> HTMLResponse:
    manager = _manager(request)
    payload = manager.player_rows(instance_id)
    return TEMPLATES.TemplateResponse(
        request,
        "partials/players.html",
        {
            "record": manager.get(instance_id),
            "players": payload["players"],
            "lists": payload["lists"],
            "online_count": payload["online_count"],
            "message": message,
            "error": error,
        },
    )


# Kept under the old name so existing call sites read naturally.
_lists_partial = _players_partial


@router.get("/api/instances/{instance_id}/players", response_class=HTMLResponse)
async def get_players(request: Request, instance_id: str) -> HTMLResponse:
    return _players_partial(request, instance_id)


@router.post("/api/instances/{instance_id}/permitted", response_class=HTMLResponse)
async def toggle_permitted(
    request: Request, instance_id: str, enabled: str = Form(default="")
) -> HTMLResponse:
    """Turn the whitelist on, or off again without losing the list."""
    try:
        _manager(request).set_permitted_enabled(instance_id, bool(enabled))
    except (PlayerListError, OSError) as exc:
        return _players_partial(request, instance_id, error=str(exc))
    state = "on" if enabled else "off"
    detail = (
        "only permitted players may join"
        if enabled
        else "anyone may join; the list is kept for later"
    )
    return _players_partial(
        request, instance_id, message=f"Whitelist {state} — {detail}."
    )


@router.post("/api/instances/{instance_id}/players/{player_id}/forget", response_class=HTMLResponse)
async def forget_player(request: Request, instance_id: str, player_id: str) -> HTMLResponse:
    removed = _manager(request).forget_player(instance_id, player_id)
    if not removed:
        return _players_partial(request, instance_id, error="That player is not on the roster.")
    return _players_partial(
        request, instance_id,
        message=f"Removed {player_id} from the roster. Their list entries are unchanged.",
    )


@router.get("/api/instances/{instance_id}/players/{player_id}/history")
async def player_history(request: Request, instance_id: str, player_id: str):
    text, filename = _manager(request).player_history(instance_id, player_id)
    return PlainTextResponse(
        text, headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@router.post("/api/instances/{instance_id}/lists", response_class=HTMLResponse)
async def mutate_list_form(
    request: Request,
    instance_id: str,
    key: str = Form(...),
    player_id: str = Form(...),
    action: str = Form(default="add"),
) -> HTMLResponse:
    """Same as the path form, with the list picked by a select in the panel."""
    return await mutate_list(
        request, instance_id, key, player_id=player_id, action=action
    )


@router.post("/api/instances/{instance_id}/lists/{key}", response_class=HTMLResponse)
async def mutate_list(
    request: Request,
    instance_id: str,
    key: str,
    player_id: str = Form(...),
    action: str = Form(default="add"),
) -> HTMLResponse:
    record = _manager(request).get(instance_id)
    try:
        target = record.lists.by_key(key)
        if action == "remove":
            changed = target.remove(player_id)
            verb = "removed from" if changed else "was not on"
        else:
            changed = target.add(player_id)
            verb = "added to" if changed else "was already on"
        if key == "banned" and action == "remove":
            # Drop any pending kick expiry so it cannot re-ban later.
            record.lists.temp_bans.cancel(player_id)
    except PlayerListError as exc:
        return _lists_partial(request, instance_id, error=str(exc))
    except OSError as exc:
        return _lists_partial(request, instance_id, error=f"Could not write the list: {exc}")
    return _lists_partial(request, instance_id, message=f"{player_id} {verb} the {key} list.")


@router.post("/api/instances/{instance_id}/players/{player_id}/{action}", response_class=HTMLResponse)
async def moderate_player(
    request: Request, instance_id: str, player_id: str, action: str
) -> HTMLResponse:
    """Quick moderation for a connected player.

    Valheim re-reads its list files while running, so these take effect within
    seconds without a restart.
    """
    record = _manager(request).get(instance_id)
    lists = record.lists
    try:
        if action == "kick":
            # No RCON and no console input on a stock server: a brief ban is
            # the only way to disconnect someone, and it lifts itself.
            lists.kick(player_id)
            message = (
                f"Kicked {player_id}; the ban lifts automatically in "
                f"~{KICK_BAN_SECONDS:.0f}s."
            )
        elif action == "ban":
            lists.temp_bans.cancel(player_id)
            lists.banned.add(player_id)
            message = f"Banned {player_id}."
        elif action == "unban":
            lists.temp_bans.cancel(player_id)
            lists.banned.remove(player_id)
            message = f"Unbanned {player_id}."
        elif action == "admin":
            lists.admins.add(player_id)
            message = f"{player_id} is now an admin."
        elif action == "unadmin":
            lists.admins.remove(player_id)
            message = f"{player_id} is no longer an admin."
        elif action == "permit":
            lists.permitted.add(player_id)
            message = f"{player_id} added to the permitted list."
        elif action == "unpermit":
            lists.permitted.remove(player_id)
            message = f"{player_id} removed from the permitted list."
        else:
            return _lists_partial(request, instance_id, error=f"Unknown action {action!r}.")
    except PlayerListError as exc:
        return _lists_partial(request, instance_id, error=str(exc))
    except OSError as exc:
        return _lists_partial(request, instance_id, error=f"Could not write the list: {exc}")
    return _lists_partial(request, instance_id, message=message)


@router.get("/api/instances/{instance_id}/connectivity", response_class=HTMLResponse)
async def connectivity(request: Request, instance_id: str) -> HTMLResponse:
    """Report what a Valheim client would see when it probes this server.

    A client reads a server's name, player count and version from the Steam
    query socket, not the game port, so a server can accept direct joins while
    its listing looks dead. The probe reports the UDP sockets the process
    actually holds, so a missing query socket can be told apart from one bound
    somewhere the probe was not looking.
    """
    manager = _manager(request)
    record = manager.get(instance_id)
    probe = await manager.probe_instance(record)
    return TEMPLATES.TemplateResponse(
        request, "partials/connectivity.html", {"probe": probe}
    )


@router.get("/api/update")
async def api_update_status(request: Request, force: str = "") -> JSONResponse:
    return JSONResponse(await _manager(request).check_update(force=bool(force)))


# --------------------------------------------------------------------------- #
# instance export / import / clone
#
# A server *instance* is the world plus everything around it -- configuration,
# players, access lists, mods and snapshots -- so these three move all of it.
# Moving a world on its own lives further down under "world import / export".
# --------------------------------------------------------------------------- #
@router.get("/api/instances/{instance_id}/export")
async def export_instance_archive(
    request: Request,
    instance_id: str,
    include_logs: str = "",
):
    """Download the whole instance as a single archive."""
    manager = _manager(request)
    path = await asyncio.to_thread(
        manager.export_archive, instance_id, include_logs=bool(include_logs)
    )

    def cleanup() -> None:
        shutil.rmtree(path.parent, ignore_errors=True)

    return FileResponse(
        path,
        media_type="application/zip",
        filename=path.name,
        # The archive is built into a temp directory; remove it once sent.
        background=BackgroundTask(cleanup),
    )


@router.post("/api/instances/import", response_class=HTMLResponse)
async def import_instance_archive(
    request: Request, file: UploadFile = File(...), name: str = Form(default="")
) -> HTMLResponse:
    """Create an instance from an uploaded archive."""
    if not (file.filename or "").lower().endswith((SUFFIX, ".zip")):
        return HTMLResponse(
            f'<div class="alert error">Please upload a {SUFFIX} archive.</div>', 400
        )

    staging = Path(tempfile.mkdtemp(prefix="vhsm-import-"))
    target = staging / "upload.zip"
    total = 0
    try:
        with target.open("wb") as sink:
            while chunk := await file.read(1 << 20):
                total += len(chunk)
                if total > MAX_ARCHIVE_UPLOAD:
                    return HTMLResponse(
                        '<div class="alert error">That archive is too large.</div>', 400
                    )
                sink.write(chunk)
        record = _manager(request).import_archive(target, name=name)
    except (ArchiveError, ValidationError) as exc:
        return HTMLResponse(f'<div class="alert error">{exc}</div>', 400)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return HTMLResponse(
        "", headers={"HX-Redirect": f"/instances/{record.config.id}"}
    )


@router.post("/api/instances/{instance_id}/clone", response_class=HTMLResponse)
async def clone_instance(request: Request, instance_id: str) -> HTMLResponse:
    """Duplicate an instance and open the copy.

    Deliberately not started: two servers loading the same world would be two
    servers fighting over the same save.
    """
    manager = _manager(request)
    try:
        record = await manager.clone(instance_id)
    except (ManagerError, ValidationError, OSError) as exc:
        # 200 with a banner rather than a 4xx: htmx only swaps successful
        # responses, and an error nobody can see is worse than no error.
        return HTMLResponse(f'<div class="alert error">{exc}</div>')
    return HTMLResponse(
        "", headers={"HX-Redirect": f"/instances/{record.config.id}"}
    )


# --------------------------------------------------------------------------- #
# world backups / rollback
# --------------------------------------------------------------------------- #
def _backups_partial(
    request: Request, instance_id: str, message: str = "", error: str = ""
) -> HTMLResponse:
    manager = _manager(request)
    return TEMPLATES.TemplateResponse(
        request,
        "partials/backups.html",
        {
            "record": manager.get(instance_id),
            "backups": manager.backup_summary(instance_id),
            "message": message,
            "error": error,
        },
    )


def _transfer_partial(
    request: Request, instance_id: str, message: str = "", error: str = ""
) -> HTMLResponse:
    """The world import/export panel.

    It needs the same backup summary as the Backups panel -- chiefly whether the
    server is running, which decides if importing is allowed at all.
    """
    manager = _manager(request)
    return TEMPLATES.TemplateResponse(
        request,
        "partials/transfer.html",
        {
            "record": manager.get(instance_id),
            "backups": manager.backup_summary(instance_id),
            "message": message,
            "error": error,
        },
    )


@router.get("/api/instances/{instance_id}/backups", response_class=HTMLResponse)
async def get_backups(request: Request, instance_id: str) -> HTMLResponse:
    return _backups_partial(request, instance_id)


@router.get("/api/instances/{instance_id}/transfer", response_class=HTMLResponse)
async def get_transfer(request: Request, instance_id: str) -> HTMLResponse:
    return _transfer_partial(request, instance_id)


@router.post("/api/instances/{instance_id}/backups/snapshot", response_class=HTMLResponse)
async def snapshot_world(request: Request, instance_id: str) -> HTMLResponse:
    try:
        made = _manager(request).snapshot_world(instance_id)
    except (BackupError, OSError) as exc:
        return _backups_partial(request, instance_id, error=str(exc))
    return _backups_partial(request, instance_id, message=f"Snapshot taken: {made.label}.")


@router.post("/api/instances/{instance_id}/backups/restore", response_class=HTMLResponse)
async def restore_world(
    request: Request, instance_id: str, key: str = Form(...)
) -> HTMLResponse:
    try:
        restored = _manager(request).restore_world(instance_id, key)
    except (BackupError, OSError) as exc:
        return _backups_partial(request, instance_id, error=str(exc))
    return _backups_partial(
        request,
        instance_id,
        message=(
            f"Rolled back to {restored.label}. The world as it was just now was "
            "saved as a new snapshot first, so this can be undone."
        ),
    )


@router.post("/api/instances/{instance_id}/backups/delete", response_class=HTMLResponse)
async def delete_backup(
    request: Request, instance_id: str, key: str = Form(...)
) -> HTMLResponse:
    try:
        _manager(request).delete_backup(instance_id, key)
    except (BackupError, OSError) as exc:
        return _backups_partial(request, instance_id, error=str(exc))
    return _backups_partial(request, instance_id, message="Backup deleted.")


# --------------------------------------------------------------------------- #
# world upload
# --------------------------------------------------------------------------- #
def _safe_member(filename: str) -> Path | None:
    """Turn an uploaded file's name into a safe relative path.

    A directory upload sends each file's path relative to the chosen folder, so
    the name can contain separators and has to be validated like any other
    archive member.
    """
    raw = (filename or "").replace("\\", "/").strip()
    if not raw:
        return None
    parts = [p for p in PurePosixPath(raw).parts if p not in ("", ".", "/")]
    if any(p == ".." for p in parts) or not parts:
        return None
    return Path(*parts)


@router.get("/api/instances/{instance_id}/world/export")
async def export_world_archive(request: Request, instance_id: str):
    """Download this instance's live world as a zip.

    The world only -- configuration, players and mods stay behind, because
    this is the file you hand to someone who wants to play *this map* on their
    own server. Moving the server itself is the instance archive.
    """
    manager = _manager(request)
    try:
        path = await asyncio.to_thread(manager.export_world_archive, instance_id)
    except (WorldError, OSError) as exc:
        return PlainTextResponse(str(exc) + "\n", status_code=404)

    def cleanup() -> None:
        shutil.rmtree(path.parent, ignore_errors=True)

    return FileResponse(
        path,
        media_type="application/zip",
        filename=path.name,
        background=BackgroundTask(cleanup),
    )


@router.post("/api/instances/{instance_id}/world/upload", response_class=HTMLResponse)
async def upload_world(
    request: Request,
    instance_id: str,
    files: list[UploadFile] = File(...),
    name: str = Form(default=""),
    confirm: str = Form(default=""),
    overwrite: str = Form(default=""),
    adopt_name: str = Form(default="1"),
) -> HTMLResponse:
    """Install a world from a zipped world folder or the folder's files.

    ``confirm`` is the operator agreeing to overwrite a world that is already
    there; ``overwrite`` is the older spelling of the same thing, kept so a
    scripted caller does not break.
    """
    manager = _manager(request)
    replace = bool(confirm) or bool(overwrite)
    previous_world = manager.get(instance_id).config.world
    staging = Path(tempfile.mkdtemp(prefix="vhsm-world-"))
    unpack = staging / "unpacked"
    unpack.mkdir()

    try:
        uploads = [f for f in files if (f.filename or "").strip()]
        if not uploads:
            return _transfer_partial(request, instance_id, error="No files were uploaded.")

        single_zip = len(uploads) == 1 and uploads[0].filename.lower().endswith(".zip")
        total = 0
        if single_zip:
            payload = staging / "world.zip"
            with payload.open("wb") as sink:
                while chunk := await uploads[0].read(1 << 20):
                    total += len(chunk)
                    if total > MAX_ARCHIVE_UPLOAD:
                        return _transfer_partial(
                            request, instance_id, error="That upload is too large."
                        )
                    sink.write(chunk)
            try:
                extract_zip(payload, unpack)
            except ArchiveError as exc:
                return _transfer_partial(request, instance_id, error=str(exc))
        else:
            for upload in uploads:
                relative = _safe_member(upload.filename)
                if relative is None:
                    return _transfer_partial(
                        request, instance_id,
                        error=f"Refusing a file with an unsafe name: {upload.filename!r}",
                    )
                destination = (unpack / relative).resolve()
                if unpack.resolve() not in destination.parents:
                    return _transfer_partial(
                        request, instance_id,
                        error=f"Refusing a file that escapes the upload: {upload.filename!r}",
                    )
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("wb") as sink:
                    while chunk := await upload.read(1 << 20):
                        total += len(chunk)
                        if total > MAX_ARCHIVE_UPLOAD:
                            return _transfer_partial(
                                request, instance_id, error="That upload is too large."
                            )
                        sink.write(chunk)

        try:
            result = await asyncio.to_thread(
                partial(
                    manager.install_world,
                    instance_id,
                    unpack,
                    name=name.strip(),
                    confirm=replace,
                    adopt_name=bool(adopt_name),
                )
            )
        except (WorldError, BackupError, ValidationError) as exc:
            return _transfer_partial(request, instance_id, error=str(exc))
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    installed = result.world
    record = manager.get(instance_id)
    message = (
        f"Installed world {installed.name!r} "
        f"({installed.file_count} files, {installed.format} format)."
    )
    if result.replaced:
        # Say plainly when the upload was called something else: it now lives
        # under this server's world name, which is what makes it a replacement.
        came_as = (
            f" {result.source_name} was installed as {installed.name}, which is the "
            "world this server loads."
            if result.source_name and result.source_name != installed.name
            else ""
        )
        message += (
            f"{came_as} The world that was here has been replaced; it was "
            "snapshotted first, so it can be rolled back to under Backups."
        )
    renamed = record.config.world == installed.name and previous_world != installed.name
    if renamed:
        message += (
            f" This instance's world name was changed from {previous_world!r} so it "
            "loads the world you uploaded."
        )
    if installed.issues:
        message += " Warning: " + "; ".join(installed.issues)

    response = _transfer_partial(request, instance_id, message=message)
    # The Backups panel is a sibling of this one and now describes a world that
    # has been replaced, so tell it to re-fetch itself.
    events: dict[str, Any] = {"vhsm:world-changed": {"world": installed.name}}
    if renamed:
        # The configuration form was rendered with the old world name and is
        # still on screen; leaving it stale would let a later save silently
        # point the server back at a world that is no longer there.
        events["vhsm:world-renamed"] = {"world": installed.name}
    response.headers["HX-Trigger"] = json.dumps(events)
    return response


@router.get("/api/instances/{instance_id}/console-log")
async def console_log(request: Request, instance_id: str):
    """Download one instance's complete console transcript.

    The page shows the tail and the in-memory buffer is trimmed, so the file on
    disk is the only full record of what a server did -- which is what a crash
    that scrolled past needs.
    """
    record = _manager(request).get(instance_id)
    filename = f"{record.config.slug}-console.log"
    path = record.layout.console_log
    if not path.is_file():
        return PlainTextResponse(
            f"{record.config.name} has not written any console output yet.\n",
            status_code=404,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    return FileResponse(path, media_type="text/plain", filename=filename)


# --------------------------------------------------------------------------- #
# steamcmd log and updates
# --------------------------------------------------------------------------- #
@router.get("/api/steamcmd-log")
async def steamcmd_log(request: Request):
    """Download the full steamcmd transcript.

    The in-memory tail is trimmed, so the file is the only complete record of a
    long install -- which is what is needed when one fails.
    """
    manager = _manager(request)
    path = manager.job.path
    if path is None or not path.is_file():
        return PlainTextResponse(
            "No steamcmd output has been recorded yet.\n",
            status_code=404,
            headers={"Content-Disposition": 'attachment; filename="steamcmd.log"'},
        )
    return FileResponse(
        path, media_type="text/plain", filename="steamcmd.log"
    )


@router.post("/api/update/run", response_class=HTMLResponse)
async def run_update_now(request: Request) -> HTMLResponse:
    """Update the shared server files, restarting instances around it."""
    manager = _manager(request)
    if manager.job.running:
        return HTMLResponse('<div class="alert info">An update is already running.</div>')
    asyncio.create_task(manager.run_update(restart=True))
    await asyncio.sleep(0.3)
    return HTMLResponse(
        '<div class="alert info">Update started. Watch it on the '
        '<a href="/settings">Settings</a> page.</div>'
    )
