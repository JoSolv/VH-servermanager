"""JSON API plus the HTMX endpoints that drive the mod manager.

Mod mutations return the re-rendered mod list, because that is what the page
needs; read-only endpoints under ``/api`` return JSON so the manager can also
be scripted.
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.background import BackgroundTask

from ..instance import InstanceConfig, ValidationError
from ..playerlists import KICK_BAN_SECONDS, PlayerListError
from ..archive import ArchiveError, SUFFIX
from ..backups import BackupError
from ..mods.cache import cache_size, clear_cache
from ..mods.profile import ModError
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
    profile = manager.profile(instance_id)
    return TEMPLATES.TemplateResponse(
        request,
        "partials/installed_mods.html",
        {
            "record": manager.get(instance_id),
            "mods": profile.summary(),
            "orphans": [m.package_full_name for m in profile.orphans()],
            "message": message,
            "error": error,
        },
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
def _lists_partial(
    request: Request, instance_id: str, message: str = "", error: str = ""
) -> HTMLResponse:
    record = _manager(request).get(instance_id)
    return TEMPLATES.TemplateResponse(
        request,
        "partials/player_lists.html",
        {
            "record": record,
            "lists": record.lists.summary(),
            "message": message,
            "error": error,
        },
    )


@router.get("/api/instances/{instance_id}/lists", response_class=HTMLResponse)
async def get_lists(request: Request, instance_id: str) -> HTMLResponse:
    return _lists_partial(request, instance_id)


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
# instance export / import
# --------------------------------------------------------------------------- #
@router.get("/api/instances/{instance_id}/export")
async def export_instance_archive(
    request: Request,
    instance_id: str,
    include_mods: str = "",
    include_backups: str = "",
):
    """Download the whole instance as a single archive."""
    manager = _manager(request)
    path = manager.export_archive(
        instance_id,
        include_mods=bool(include_mods),
        include_backups=bool(include_backups),
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


@router.get("/api/instances/{instance_id}/backups", response_class=HTMLResponse)
async def get_backups(request: Request, instance_id: str) -> HTMLResponse:
    return _backups_partial(request, instance_id)


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
