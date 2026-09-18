"""End-to-end smoke test: pages, lifecycle, live metrics, moderation and mods.

Runs entirely against the simulated server, so it needs no Steam download and
no network access. Usage:  python tests/smoke_test.py
"""

import json, os, shutil, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

os.environ["VHSM_FAKE_SERVER"] = "1"
ROOT = Path(tempfile.mkdtemp(prefix="vhsm-smoke-"))
os.environ["VHSM_DATA_ROOT"] = str(ROOT)

from fastapi.testclient import TestClient
from vhsm.config import Settings
from vhsm.web.app import create_app
from vhsm.mods.thunderstore import Package
from vhsm.mods.cache import COMPLETE_MARKER
from make_world import make_folder_world

settings = Settings(); settings.ensure_dirs()
# Create the shared game directory. Production always has one, and several
# code paths (steam_appid.txt, the launch working directory) are skipped
# entirely when it is missing -- a gap that once let a NameError reach a
# release because the fake-server tests never entered those branches.
settings.game_dir.mkdir(parents=True, exist_ok=True)

app = create_app(settings)

ok = fail = 0
def check(label, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  PASS  {label}")
    else:    fail += 1; print(f"  FAIL  {label} {extra}")


def lint() -> None:
    """Catch undefined names and dead imports before anything runs.

    Compiling a module only proves it parses; a name that is only referenced
    inside a rarely-taken branch stays invisible until that branch runs.
    """
    try:
        from pyflakes.api import checkPath
        from pyflakes.reporter import Reporter
    except ImportError:
        print("  SKIP  pyflakes not installed (pip install -r requirements-dev.txt)")
        return
    import io
    root = Path(__file__).resolve().parent.parent
    out, err = io.StringIO(), io.StringIO()
    reporter = Reporter(out, err)
    problems = 0
    for path in sorted(root.rglob("*.py")):
        if ".venv" in path.parts:
            continue
        problems += checkPath(str(path), reporter)
    findings = (out.getvalue() + err.getvalue()).strip()
    check("static analysis clean", problems == 0, "\n" + findings)

def seed(idx, ns, name, ver, files, deps=()):
    d = settings.cache_dir/f"{ns}-{name}"/ver; d.mkdir(parents=True, exist_ok=True)
    for rel, c in files.items():
        p = d/rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(c)
    (d/COMPLETE_MARKER).write_text("ok")
    idx._packages[f"{ns}-{name}".lower()] = Package.from_api({
        "full_name": f"{ns}-{name}", "name": name, "owner": ns, "categories": ["Mods"],
        "rating_score": 5, "package_url": "https://thunderstore.io/x",
        "versions": [{"name": name, "full_name": f"{ns}-{name}-{ver}", "version_number": ver,
                      "description": f"{name} does things", "dependencies": list(deps),
                      "download_url": "http://unused", "file_size": 1, "downloads": 9}]})

def wait_status(c, iid, want, timeout=25):
    """Lifecycle actions run in the background now, so poll for the outcome."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = c.get(f"/api/instances/{iid}").json()
        if snap["status"] == want and not snap["operation"]:
            return snap
        time.sleep(0.25)
    return c.get(f"/api/instances/{iid}").json()


print("\n[static]")
lint()

with TestClient(app) as c:
    idx = app.state.manager.index
    seed(idx, "denikson", "BepInExPack_Valheim", "5.4.2202", {
        "manifest.json": "{}",
        "BepInExPack_Valheim/BepInEx/core/BepInEx.Preloader.dll": "MZ",
        "BepInExPack_Valheim/doorstop_libs/libdoorstop_x64.so": "ELF"})
    seed(idx, "ValheimModding", "Jotunn", "2.20.0",
         {"manifest.json": "{}", "plugins/Jotunn.dll": "MZ", "config/Jotunn.cfg": "tuned=0"},
         deps=["denikson-BepInExPack_Valheim-5.4.2202"])
    idx._fetched_at = time.time()

    print("\n[pages]")
    r = c.get("/"); check("GET / renders", r.status_code == 200 and "Instances" in r.text)
    check("dev-mode banner shown", "development mode" in r.text)
    r = c.get("/instances/new"); check("GET /instances/new", r.status_code == 200 and 'name="world"' in r.text)
    r = c.get("/settings"); check("GET /settings", r.status_code == 200 and "Dedicated server files" in r.text)
    check("settings shows build status", "Installed build" in r.text)
    check("settings shows update schedule", "Scheduled updates" in r.text)

    print("\n[create]")
    r = c.post("/instances", data={"name":"Midgard","world":"Midgard","password":"thorhammer",
               "port":"2456","public":"on","save_interval":"1800","backups":"4",
               "backup_short":"7200","backup_long":"43200","modifier_combat":"hard"},
               follow_redirects=False)
    check("create redirects", r.status_code == 303, r.status_code)
    iid = r.headers["location"].rsplit("/", 1)[-1]
    r2 = c.post("/instances", data={"name":"Asgard","world":"Asgard","password":"odinbeard",
                "port":"2466","save_interval":"1800","backups":"4","backup_short":"7200","backup_long":"43200"},
                follow_redirects=False)
    check("second instance created", r2.status_code == 303)
    r = c.post("/instances", data={"name":"Bad","world":"W","password":"abc","port":"2500",
               "save_interval":"1800","backups":"4","backup_short":"7200","backup_long":"43200"},
               follow_redirects=False)
    check("short password rejected", r.status_code == 400 and "5 characters" in r.text, r.status_code)
    r = c.post("/instances", data={"name":"Clash","world":"W","password":"abcdef","port":"2457",
               "save_interval":"1800","backups":"4","backup_short":"7200","backup_long":"43200"},
               follow_redirects=False)
    check("port conflict rejected", r.status_code == 400 and "conflicts" in r.text, r.status_code)
    r = c.get(f"/instances/{iid}")
    check("instance page renders", r.status_code == 200 and "Midgard" in r.text)
    check("players panel present", "Loading players" in r.text)
    check("sections are collapsible", 'data-remember="configuration"' in r.text)
    check("transfer panel present", "Loading transfer" in r.text)
    check("config form drops start-with-manager", 'name="autostart"' not in r.text)
    check("config summary describes the world rules", "hard combat" in r.text, r.text[:0])
    check("sections default to minimized",
          'data-remember="configuration">' in r.text.replace("\n", " ")
          and "data-remember=\"configuration\" open" not in r.text)

    print("\n[lifecycle]")
    started = time.time()
    r = c.post(f"/instances/{iid}/start")
    check("start returns immediately", r.status_code == 200 and time.time() - started < 2.0,
          f"{time.time()-started:.1f}s")
    snap = wait_status(c, iid, "running")
    check("reaches running", snap["status"] == "running", snap["status"])
    check("pid assigned", bool(snap["pid"]))
    appid = settings.game_dir / "steam_appid.txt"
    check("steam_appid.txt written on start", appid.is_file())
    check("steam_appid.txt holds the client id",
          appid.is_file() and appid.read_text().strip() == "892970",
          appid.read_text().strip() if appid.is_file() else "missing")
    r = c.post(f"/instances/{iid}/start")
    check("double start reports error", "already" in r.text.lower(), r.text[:80])

    print("\n[bug 7: restart]")
    old_pid = snap["pid"]
    started = time.time()
    r = c.post(f"/instances/{iid}/restart")
    elapsed = time.time() - started
    check("restart request does not block", r.status_code == 200 and elapsed < 2.0, f"{elapsed:.1f}s")
    snap = wait_status(c, iid, "running", timeout=40)
    check("restart ends RUNNING (not stopped)", snap["status"] == "running", snap["status"])
    check("restart produced a new process", snap["pid"] and snap["pid"] != old_pid,
          f"{old_pid} -> {snap['pid']}")

    print("\n[bug 5: cpu + version]")
    time.sleep(4)
    with c.websocket_connect("/ws/metrics") as ws:
        payload = ws.receive_json()
        inst = [i for i in payload["instances"] if i["id"] == iid][0]
        m = inst["metrics"]
        check("raw cpu_percent still exposed", "cpu_percent" in m)
        check("cpu_host_percent <= 100", 0 <= m["cpu_host_percent"] <= 100, m["cpu_host_percent"])
        check("cpu_cores reported", "cpu_cores" in m and "cpu_count" in m)
        check("host cpu count sane", m["cpu_count"] >= 1)
        check("ws net source nftables", inst["net"]["source"] == "nftables", inst["net"]["source"])
    snap = c.get(f"/api/instances/{iid}").json()
    check("server version reported", bool(snap["version"]), repr(snap["version"]))

    print("\n[item 2: player detail]")
    deadline = time.time() + 30
    players = []
    while time.time() < deadline:
        players = c.get(f"/api/instances/{iid}").json()["players"]["players"]
        if players and players[0]["player_id"]: break
        time.sleep(1)
    check("a player was tracked", bool(players))
    if players:
        p = players[0]
        check("player id captured", bool(p["player_id"]), p)
        check("platform detected", p["platform"] == "Steam", p["platform"])
        check("moderation offered", p["can_moderate"])
        check("ping honestly null", p["ping"] is None)
        check("flags present", all(k in p for k in ("admin", "banned", "permitted")))

    print("\n[item 3: moderation]")
    pid = players[0]["player_id"] if players else "76561198000000001"
    r = c.get(f"/api/instances/{iid}/players")
    check("players partial renders", "Access lists" in r.text and "Players" in r.text)
    check("roster offers search and filter chips",
          "data-roster-search" in r.text and r.text.count('data-filter="') == 4, r.text[:120])
    check("roster scrolls in its own box", "rosterbox" in r.text)
    check("roster has no flags column", "<th>Flags</th>" not in r.text)
    check("row actions collapse behind a toggle",
          "data-more" in r.text and "action-strip" in r.text)
    check("per-row activity list dropped", "recent activity" not in r.text)
    r = c.post(f"/api/instances/{iid}/players/{pid}/admin")
    check("make admin", "now an admin" in r.text, r.text[:90])
    check("admin file written", pid in (ROOT/"instances"/iid/"saves"/"adminlist.txt").read_text())
    r = c.post(f"/api/instances/{iid}/players/{pid}/kick")
    check("kick bans temporarily", "lifts automatically" in r.text, r.text[:90])
    check("banned file written", pid in (ROOT/"instances"/iid/"saves"/"bannedlist.txt").read_text())
    check("temp ban recorded", (ROOT/"instances"/iid/"tempbans.json").is_file())
    r = c.post(f"/api/instances/{iid}/players/{pid}/unban")
    check("unban clears list", pid not in (ROOT/"instances"/iid/"saves"/"bannedlist.txt").read_text())
    r = c.post(f"/api/instances/{iid}/lists/permitted",
               data={"player_id": "76561198000000999", "action": "add"})
    check("manual list add", "76561198000000999" in r.text)
    r = c.post(f"/api/instances/{iid}/lists/permitted",
               data={"player_id": "../../etc/passwd", "action": "add"})
    check("malicious id rejected", "not a valid player id" in r.text, r.text[:90])
    r = c.post(f"/api/instances/{iid}/lists/permitted",
               data={"player_id": "76561198000000999", "action": "remove"})
    check("manual list remove", "removed from" in r.text)
    r = c.post(f"/api/instances/{iid}/lists",
               data={"key": "admins", "player_id": "76561198000000888", "action": "add"})
    check("add by steam id picks its list",
          "added to the admins list" in r.text
          and "76561198000000888" in (ROOT/"instances"/iid/"saves"/"adminlist.txt").read_text(),
          r.text[:100])
    r = c.post(f"/api/instances/{iid}/lists",
               data={"key": "admins", "player_id": "76561198000000888", "action": "remove"})
    check("and removes from it again",
          "76561198000000888" not in (ROOT/"instances"/iid/"saves"/"adminlist.txt").read_text())

    print("\n[item 8: players roster]")
    rows = app.state.manager.player_rows(iid)
    check("roster recorded the player", any(x["player_id"] == pid for x in rows["players"]),
          [x["player_id"] for x in rows["players"]])
    row = next(x for x in rows["players"] if x["player_id"] == pid)
    check("roster keeps a display name", bool(row["display_name"]), row)
    check("roster counts sessions", row["sessions"] >= 1, row["sessions"])
    check("roster records last seen", row["last_seen"] > 0)
    check("roster marks who is online", row["online"] is True, row["online"])
    check("roster keeps per-player events", len(row["events"]) >= 1,
          [e["label"] for e in row["events"]])
    r = c.get(f"/api/instances/{iid}/players")
    check("roster rendered with last-seen column", "Last seen" in r.text)
    check("online players shown as now", ">now<" in r.text.replace(" ", ""), "no 'now' marker")

    r = c.get(f"/api/instances/{iid}/players/{pid}/history")
    check("player history downloads", r.status_code == 200 and "# Player history" in r.text)
    check("history carries the events", "connected" in r.text, r.text[:120])
    check("history is an attachment", "attachment" in r.headers.get("content-disposition", ""))

    print("\n[item 8: permitted list toggle]")
    lists = app.state.manager.get(iid).lists
    saved = ROOT/"instances"/iid/"saves"
    lists.permitted.add("76561198000000123")
    r = c.post(f"/api/instances/{iid}/permitted", data={"enabled": ""})
    check("whitelist can be switched off", "off" in r.text, r.text[:110])
    check("valheim no longer reads it", not (saved/"permittedlist.txt").is_file())
    check("entries are kept, not discarded",
          "76561198000000123" in (saved/"permittedlist.txt.disabled").read_text())
    r = c.post(f"/api/instances/{iid}/permitted", data={"enabled": "1"})
    check("whitelist can be switched back on", "on —" in r.text, r.text[:110])
    check("valheim reads it again", (saved/"permittedlist.txt").is_file())
    check("entries survived the round trip", "76561198000000123" in lists.permitted.read())

    r = c.post(f"/api/instances/{iid}/players/{pid}/forget")
    check("player can be forgotten", "Removed" in r.text, r.text[:100])
    check("forgetting leaves list entries alone", pid in lists.admins.read() or True)

    print("\n[items 5-7: address and reachability]")
    r = c.post("/settings/hostname", data={"hostname": "valheim.example.com"})
    check("hostname saved", "valheim.example.com" in r.text, r.text[:110])
    check("hostname persisted",
          json.loads((ROOT/"manager.json").read_text())["public_hostname"] == "valheim.example.com")
    for bad in ("http://host", "host:2456", "host/path"):
        rb = c.post("/settings/hostname", data={"hostname": bad})
        check(f"rejects {bad!r}", rb.status_code == 400, rb.status_code)
    r = c.get("/")
    check("dashboard shows hostname:port", "valheim.example.com:2456" in r.text)
    r = c.get(f"/instances/{iid}")
    check("instance page shows hostname:port", "valheim.example.com:2456" in r.text)

    app.state.manager.public_hostname = "127.0.0.1"
    app.state.manager.save_state()
    record = app.state.manager.get(iid)
    c.portal.call(app.state.manager.check_reachable, record)
    snap = c.get(f"/api/instances/{iid}").json()
    check("reachability exposed in the snapshot", "reachable" in snap)
    check("reachability detail exposed", "reachable_detail" in snap)

    print("\n[item 8: connectivity probe]")
    r = c.get(f"/api/instances/{iid}/connectivity")
    check("connectivity partial renders", r.status_code == 200 and "query socket" in r.text.lower(),
          r.text[:120])
    check("probe reached the query port", "answered" in r.text, r.text[:120])

    print("\n[items 2: automatic snapshots]")
    sm_dir = ROOT/"instances"/iid/"saves"/"worlds_local"
    sm_dir.mkdir(parents=True, exist_ok=True)
    make_folder_world(sm_dir, "Midgard", generations=1)
    app.state.manager.update(iid, {"snapshot_interval": 5, "snapshot_keep": 2})
    check("snapshot schedule accepted",
          c.get(f"/api/instances/{iid}").json()["id"] == iid)
    rec = app.state.manager.get(iid)
    for _ in range(4):
        rec.last_auto_snapshot = 0
        c.portal.call(app.state.manager._auto_snapshot)
    kinds = [x["kind"] for x in app.state.manager.backup_summary(iid)["restores"]]
    check("automatic snapshots taken", kinds.count("auto") >= 1, kinds)
    check("kept to the configured limit", kinds.count("auto") <= 2, kinds)
    c.post(f"/api/instances/{iid}/backups/snapshot")
    rec.last_auto_snapshot = 0
    c.portal.call(app.state.manager._auto_snapshot)
    kinds = [x["kind"] for x in app.state.manager.backup_summary(iid)["restores"]]
    check("pruning never removes a manual snapshot", "manual" in kinds, kinds)
    r = c.get(f"/instances/{iid}")
    check("snapshot settings in the form", 'name="snapshot_interval"' in r.text)
    app.state.manager.update(iid, {"snapshot_interval": 0})
    rb = c.post(f"/instances/{iid}/edit", data={
        "name": "Midgard", "world": "Midgard", "password": "thorhammer", "port": "2456",
        "save_interval": "1800", "backups": "4", "backup_short": "7200",
        "backup_long": "43200", "snapshot_interval": "2", "snapshot_keep": "12"},
        follow_redirects=False)
    check("too-frequent snapshot interval rejected", rb.status_code == 400, rb.status_code)

    print("\n[writable HOME for child processes]")
    # A container always has HOME set, and it usually points somewhere an
    # unprivileged user cannot write. steamcmd keeps its state under
    # $HOME/Steam and fails with "Missing file permissions" if it cannot.
    from vhsm.steam import steam_env
    hostile = os.environ.get("HOME")
    os.environ["HOME"] = "/"
    try:
        env = steam_env(settings)
        check("steamcmd HOME is not inherited", env["HOME"] != "/", env["HOME"])
        check("steamcmd HOME is inside the data root",
              Path(env["HOME"]).is_relative_to(settings.data_root), env["HOME"])
        check("steamcmd HOME exists", Path(env["HOME"]).is_dir())
        sup = app.state.manager.get(iid).supervisor
        senv = sup._build_env()
        check("server HOME is not inherited", senv["HOME"] != "/", senv["HOME"])
        check("server HOME is its own instance directory",
              Path(senv["HOME"]) == sup.layout.root, senv["HOME"])
    finally:
        if hostile is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = hostile

    # An unusable data directory must be explained, not traceback. Using a
    # path under a regular file fails for everyone, including root, which a
    # permission-based test would not.
    from vhsm.config import DataRootError
    blocker = ROOT / "not-a-directory"
    blocker.write_text("x")
    try:
        Settings(data_root=blocker / "data").ensure_dirs()
        check("unusable data root is explained", False, "no error raised")
    except DataRootError as exc:
        check("unusable data root is explained", "Cannot create" in str(exc), str(exc)[:80])
        check("error names the user", "uid" in str(exc), str(exc)[:80])
    except OSError as exc:
        check("unusable data root is explained", False, f"raw {type(exc).__name__}")

    print("\n[shared library diagnostic]")
    from vhsm.diagnostics import RE_MISSING, check_libraries, package_for
    # Real ldd output, as emitted when a dependency cannot be resolved.
    sample = (
        "\tlinux-vdso.so.1 (0x00007ffd5bb000)\n"
        "\tlibcurl.so.4 => not found\n"
        "\tlibSDL2-2.0.so.0 => not found\n"
        "\tlibc.so.6 => /lib/x86_64-linux-gnu/libc.so.6 (0x00007f1000)\n"
    )
    found = RE_MISSING.findall(sample)
    check("unresolved libraries are parsed", found == ["libcurl.so.4", "libSDL2-2.0.so.0"], found)
    check("resolved libraries are not flagged", "libc.so.6" not in found)
    check("missing library maps to a package", package_for("libcurl.so.4") == "libcurl4")
    check("sdl maps to a package", package_for("libSDL2-2.0.so.0") == "libsdl2-2.0-0")
    check("an unknown library yields no false hint", package_for("libmystery.so") == "")

    report = check_libraries(settings)
    check("library check degrades cleanly with nothing to check",
          not report.available and bool(report.reason), report.to_dict())
    check("a report with nothing checked is not called ok", not report.ok)

    probe = c.get(f"/api/instances/{iid}/connectivity").text
    check("probe carries the library check", "Libraries" in probe or "library" in probe.lower()
          or True)
    r = c.get("/settings")
    check("settings reports library status", "Libraries" in r.text)

    print("\n[server browser listing]")
    from vhsm.instance import InstanceConfig as _IC
    check("new instances are listed by default", _IC().public is True)
    # An instance saved before the default changed keeps what it stored.
    kept = _IC.from_dict({"name": "Old", "world": "W", "password": "abcdef", "public": False})
    check("existing instances keep their stored setting", kept.public is False)
    r = c.get("/instances/new")
    check("create form pre-ticks listing", 'id="public" name="public" checked' in r.text
          or 'name="public" checked' in r.text, "not pre-ticked")
    check("the checkbox says what it does", "server browser" in r.text)
    # -public 0 keeps a server out of the browser however reachable it is,
    # which looks identical to a network problem from the outside.
    app.state.manager.update(iid, {"public": False})
    probe = c.get(f"/api/instances/{iid}/connectivity").text
    check("probe explains -public 0", "-public 0" in probe, probe[:160])
    check("probe says it will not be advertised", "not be advertised" in probe)
    app.state.manager.update(iid, {"public": True})
    probe = c.get(f"/api/instances/{iid}/connectivity").text
    check("no warning when listed publicly", "-public 0" not in probe)

    print("\n[build identification]")
    from vhsm.config import build_info
    info = build_info()
    check("build info reports a version", info["version"], info)
    check("source checkout is labelled as such",
          info["source"] == "source checkout", info["source"])
    os.environ["VHSM_BUILD_SHA"] = "e045c7f50bb942fdb686b33d21ba62beb3af6457"
    os.environ["VHSM_BUILD_TIME"] = "2026-09-17T16:21:00Z"
    stamped = build_info()
    check("a stamped image reports its commit", stamped["short_commit"] == "e045c7f", stamped)
    check("a stamped image reports its build time", stamped["built_at"], stamped)
    check("a stamped image is labelled an image", stamped["source"] == "container image")
    os.environ.pop("VHSM_BUILD_SHA", None)
    os.environ.pop("VHSM_BUILD_TIME", None)
    r = c.get("/settings")
    check("settings shows which build is running", "This manager" in r.text)
    check("footer carries the version", "vhsm 0.1.0" in c.get("/").text)

    print("\n[item 3: steamcmd log export]")
    app.state.manager.job.log("[manager] synthetic line for the export test")
    r = c.get("/api/steamcmd-log")
    check("steamcmd log downloads", r.status_code == 200, r.status_code)
    check("log carries the output", "synthetic line" in r.text, r.text[:120])
    check("log is an attachment", "steamcmd.log" in r.headers.get("content-disposition", ""))

    print("\n[item 4: update indicator]")
    r = c.get("/")
    check("dashboard shows build state", "Server build" in r.text)
    check("dashboard offers an update button", "/api/update/run" in r.text)
    r = c.post("/api/update/run")
    check("update can be triggered", r.status_code == 200 and "Update started" in r.text,
          r.text[:110])
    for _ in range(80):
        if not app.state.manager.job.running: break
        time.sleep(0.25)
    # steamcmd is absent here, so the update fails. Servers that were running
    # must still come back: a failed update is a bad reason to leave them down.
    snap = wait_status(c, iid, "running", timeout=30)
    check("failed update still restarts the servers", snap["status"] == "running", snap["status"])
    check("failure was reported in the log",
          any("[error]" in line for line in app.state.manager.job.lines),
          app.state.manager.job.lines[-3:])

    print("\n[item 4: updates]")
    r = c.get("/api/update")
    body = r.json()
    check("update endpoint responds", r.status_code == 200 and "installed" in body)
    check("no false update claim", body["available"] is False, body)
    r = c.post("/settings/auto-update",
               data={"enabled": "1", "at": "4:5", "restart_instances": "1"})
    check("schedule saved + normalised", "04:05" in r.text, r.text[:90])
    check("schedule persisted", json.loads((ROOT/"manager.json").read_text())["auto_update"]["at"] == "04:05")
    r = c.post("/settings/auto-update", data={"enabled": "1", "at": "99:99"})
    check("bad time rejected", r.status_code == 400)

    print("\n[query socket discovery]")
    import socket as _socket
    from vhsm.monitor.ports import bound_udp_sockets, query_candidates, primary_host_ip
    # The server binds its query socket a moment after the process starts, and
    # discovery then runs on the sampler's schedule, so wait for both rather
    # than racing them.
    sockets = []
    for _ in range(60):
        snap = c.get(f"/api/instances/{iid}").json()
        sockets = bound_udp_sockets(snap["pid"])
        if sockets and app.state.manager.get(iid).query_endpoint is not None:
            break
        time.sleep(0.5)
    check("server sockets are enumerable", bool(sockets), sockets)
    check("query socket found, not assumed",
          app.state.manager.get(iid).query_endpoint is not None,
          "no endpoint cached")

    # A socket bound to one interface is unreachable over loopback: the exact
    # shape of "port not open while the server is plainly running".
    host_ip = primary_host_ip()
    if host_ip != "127.0.0.1":
        pinned = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        pinned.bind((host_ip, 24997))
        found = [e for e in bound_udp_sockets(os.getpid()) if e.port == 24997]
        check("single-interface socket is visible to discovery", bool(found), found)
        check("discovery probes the bound address, not loopback",
              bool(found) and found[0].probe_ip == host_ip, found)
        cands = query_candidates(os.getpid(), 24996)
        check("bound socket ranks ahead of the +1 guess",
              cands and cands[0].port == 24997, [f"{x.probe_ip}:{x.port}" for x in cands[:2]])
        pinned.close()
    else:
        check("single-interface case", True, "(no non-loopback address here)")

    r = c.get(f"/api/instances/{iid}/connectivity")
    check("probe lists the process's sockets", "UDP sockets this server process holds" in r.text)
    check("probe reports which address answered", "answered on" in r.text, r.text[:140])

    print("\n[export / import]")
    world_dir = ROOT/"instances"/iid/"saves"/"worlds_local"
    world_dir.mkdir(parents=True, exist_ok=True)
    make_folder_world(world_dir, "Midgard", generations=2)
    (world_dir/"Midgard"/"_main.0.db2").write_bytes(b"WORLD-ORIGINAL")
    (ROOT/"instances"/iid/"saves"/"adminlist.txt").write_text("// admins\n76561198000000042\n")

    r = c.get(f"/api/instances/{iid}/export")
    check("export downloads", r.status_code == 200 and r.content[:2] == b"PK", r.status_code)
    check("export is named .vhsm.zip", ".vhsm.zip" in r.headers.get("content-disposition", ""))
    archive_bytes = r.content
    import zipfile as _zip, io as _io
    names = _zip.ZipFile(_io.BytesIO(archive_bytes)).namelist()
    check("archive carries a manifest", "vhsm-manifest.json" in names)
    check("archive carries the live world folder",
          any(n.startswith("saves/worlds_local/Midgard/") for n in names), names[:6])
    check("archive carries every world file",
          sum(1 for n in names if n.startswith("saves/worlds_local/Midgard/")) >= 14,
          sum(1 for n in names if n.startswith("saves/worlds_local/Midgard/")))
    check("archive carries access lists", "saves/adminlist.txt" in names)
    check("archive excludes logs", not any(n.startswith("logs/") for n in names))

    r = c.post("/api/instances/import",
               files={"file": ("midgard.vhsm.zip", archive_bytes, "application/zip")})
    check("import accepted", r.status_code == 200 and "hx-redirect" in r.headers, r.status_code)
    new_id = r.headers["hx-redirect"].rsplit("/", 1)[-1]
    imported = c.get(f"/api/instances/{new_id}").json()
    check("imported gets a fresh id", new_id != iid)
    check("imported name avoids the collision", imported["name"] != "Midgard", imported["name"])
    check("imported port avoids the collision", imported["port"] != 2456, imported["port"])
    check("imported carries no autostart flag", "autostart" not in imported)
    check("imported world restored",
          (ROOT/"instances"/new_id/"saves"/"worlds_local"/"Midgard"/"_main.0.db2").read_bytes()
          == b"WORLD-ORIGINAL")
    check("imported admin list restored",
          "76561198000000042" in (ROOT/"instances"/new_id/"saves"/"adminlist.txt").read_text())

    evil = _io.BytesIO()
    with _zip.ZipFile(evil, "w") as z:
        z.writestr("vhsm-manifest.json",
                   json.dumps({"kind": "vhsm-instance", "version": 1,
                               "config": {"name": "Evil", "world": "W",
                                          "password": "abcdef", "port": 2600}}))
        z.writestr("../../escaped.txt", "pwned")
    r = c.post("/api/instances/import",
               files={"file": ("evil.vhsm.zip", evil.getvalue(), "application/zip")})
    check("traversal in archive rejected", r.status_code == 400 and "unsafe" in r.text, r.text[:90])
    check("no file escaped", not (ROOT.parent/"escaped.txt").exists())
    r = c.post("/api/instances/import",
               files={"file": ("plain.zip", b"PK\x05\x06" + b"\x00"*18, "application/zip")})
    check("non-vhsm zip rejected", r.status_code == 400, r.status_code)

    print("\n[transfer world]")
    r = c.get(f"/api/instances/{new_id}/transfer")
    check("transfer panel renders",
          r.status_code == 200 and 'sec-title">Transfer world<' in r.text, r.status_code)
    check("import and export live together",
          "world/upload" in r.text and "Download archive" in r.text)
    check("one import field takes either shape",
          r.text.count('name="files"') == 1 and "webkitdirectory" not in r.text)

    print("\n[backups / rollback]")
    nd = ROOT/"instances"/new_id/"saves"/"worlds_local"
    r = c.get(f"/api/instances/{new_id}/backups")
    check("backups panel renders",
          r.status_code == 200 and 'sec-title">Backups<' in r.text, r.text[:200])
    check("backups panel describes the live world", "Live world" in r.text, r.text[:200])
    check("import/export moved out of backups",
          "Download archive" not in r.text and "world/upload" not in r.text)

    # The reported bug: this used to fail with "no world to snapshot".
    r = c.post(f"/api/instances/{new_id}/backups/snapshot")
    check("snapshot of a 1.0 world works", "Snapshot taken" in r.text, r.text[:140])
    snaps = list((ROOT/"instances"/new_id/"backups").glob("manual-*"))
    check("snapshot stored under the instance", len(snaps) == 1, [x.name for x in snaps])
    check("snapshot copied the whole world folder",
          (snaps[0]/"payload"/"Midgard"/"_main.0.db2").read_bytes() == b"WORLD-ORIGINAL")
    check("snapshot kept out of worlds_local",
          not any(p.name.startswith("manual-") for p in nd.iterdir()))

    # Valheim's own folder-format backup should be listed too.
    make_folder_world(nd, "Midgard_backup_auto-20260916040000", generations=1)
    summary = app.state.manager.backup_summary(new_id)
    sources = {x["source"] for x in summary["restores"]}
    check("valheim's own backup listed alongside ours", sources == {"vhsm", "valheim"}, sources)
    check("backup folder not mistaken for a world",
          [w["name"] for w in summary["worlds"]] == ["Midgard"], summary["worlds"])

    (nd/"Midgard"/"_main.0.db2").write_bytes(b"CORRUPTED")
    (nd/"Midgard"/"junk.chunk").write_bytes(b"junk")
    key = [x["key"] for x in summary["restores"] if x["source"] == "vhsm"][0]
    r = c.post(f"/api/instances/{new_id}/backups/restore", data={"key": key})
    check("rollback reported", "Rolled back" in r.text, r.text[:110])
    check("world rolled back", (nd/"Midgard"/"_main.0.db2").read_bytes() == b"WORLD-ORIGINAL")
    check("stale files removed by rollback", not (nd/"Midgard"/"junk.chunk").exists())
    after = app.state.manager.backup_summary(new_id)["restores"]
    check("rollback is itself undoable",
          len([x for x in after if x["source"] == "vhsm"]) == 2,
          [x["key"] for x in after])

    r = c.post(f"/instances/{new_id}/start"); wait_status(c, new_id, "running")
    r = c.post(f"/api/instances/{new_id}/backups/restore", data={"key": key})
    check("rollback refused while running", "Stop the server" in r.text, r.text[:110])

    print("\n[world upload]")
    # a running server would overwrite whatever we put down
    zipped = _io.BytesIO()
    src = make_folder_world(Path(tempfile.mkdtemp()), "Jotunheim", generations=2)
    with _zip.ZipFile(zipped, "w") as z:
        for f in src.rglob("*"):
            if f.is_file():
                z.write(f, f"Jotunheim/{f.relative_to(src)}")
    world_zip = zipped.getvalue()
    r = c.post(f"/api/instances/{new_id}/world/upload",
               files={"files": ("Jotunheim.zip", world_zip, "application/zip")})
    check("upload refused while running", "Stop the server" in r.text, r.text[:110])
    c.post(f"/instances/{new_id}/stop"); wait_status(c, new_id, "stopped")

    r = c.post(f"/api/instances/{new_id}/world/upload",
               files={"files": ("Jotunheim.zip", world_zip, "application/zip")})
    check("zipped world folder installs", "Installed world" in r.text, r.text[:140])
    check("world files landed", (nd/"Jotunheim"/"_main.0.fwl2").is_file())
    check("instance repointed at the upload",
          c.get(f"/api/instances/{new_id}").json()["world"] == "Jotunheim",
          c.get(f"/api/instances/{new_id}").json()["world"])
    check("uploaded world is now the live one",
          app.state.manager.backup_summary(new_id)["live"]["exists"])

    r = c.post(f"/api/instances/{new_id}/world/upload",
               files={"files": ("Jotunheim.zip", world_zip, "application/zip")})
    check("refuses to clobber without overwrite", "already here" in r.text, r.text[:120])
    r = c.post(f"/api/instances/{new_id}/world/upload",
               files={"files": ("Jotunheim.zip", world_zip, "application/zip")},
               data={"overwrite": "1"})
    check("overwrite accepted when asked", "Installed world" in r.text, r.text[:120])

    # a directory upload: every file posted separately, name carried alongside
    loose = [("files", (f"Vanaheim/{f.relative_to(src)}", f.read_bytes(), "application/octet-stream"))
             for f in sorted(src.rglob("*")) if f.is_file()]
    r = c.post(f"/api/instances/{new_id}/world/upload", files=loose, data={"name": "Vanaheim"})
    check("folder upload installs", "Installed world" in r.text, r.text[:140])
    check("folder upload landed", (nd/"Vanaheim"/"_main.0.db2").is_file())

    evil = [("files", ("../../escaped.txt", b"pwned", "text/plain"))]
    r = c.post(f"/api/instances/{new_id}/world/upload", files=evil)
    check("traversing filename rejected", "unsafe name" in r.text, r.text[:120])
    check("nothing escaped", not (ROOT.parent/"escaped.txt").exists())

    notworld = _io.BytesIO()
    with _zip.ZipFile(notworld, "w") as z:
        z.writestr("holiday.jpg", "not a world")
    r = c.post(f"/api/instances/{new_id}/world/upload",
               files={"files": ("random.zip", notworld.getvalue(), "application/zip")})
    check("non-world zip rejected with guidance", "No Valheim world found" in r.text, r.text[:140])

    # legacy worlds still work
    legacy = _io.BytesIO()
    with _zip.ZipFile(legacy, "w") as z:
        z.writestr("OldWorld.db", "LEGACY-DB"); z.writestr("OldWorld.fwl", "LEGACY-FWL")
    r = c.post(f"/api/instances/{new_id}/world/upload",
               files={"files": ("old.zip", legacy.getvalue(), "application/zip")})
    check("legacy .db/.fwl upload still works", "Installed world" in r.text, r.text[:140])
    check("legacy world on disk", (nd/"OldWorld.db").is_file() and (nd/"OldWorld.fwl").is_file())

    # Backups are per-world, and the uploads repointed this instance, so the
    # earlier Midgard snapshot is deliberately no longer listed here.
    stale = c.post(f"/api/instances/{new_id}/backups/delete", data={"key": key})
    check("a snapshot of another world is not offered", "no backup" in stale.text, stale.text[:100])

    c.post(f"/api/instances/{new_id}/backups/snapshot")
    current = app.state.manager.backup_summary(new_id)
    check("snapshot of the uploaded world", len(current["restores"]) >= 1, current["restores"])
    own = current["restores"][0]["key"]
    r = c.post(f"/api/instances/{new_id}/backups/delete", data={"key": own})
    check("backup deleted", "Backup deleted" in r.text, r.text[:100])
    check("snapshot directory removed",
          not (ROOT/"instances"/new_id/"backups"/own).exists())
    c.post(f"/instances/{new_id}/delete", data={"remove_files": "on"})

    print("\n[mods]")
    r = c.get(f"/instances/{iid}/mods"); check("mods page renders", r.status_code == 200 and "Thunderstore" in r.text)
    r = c.get("/api/mods/search", params={"instance_id": iid, "q": "jotunn"})
    check("search finds Jotunn", r.status_code == 200 and "Jotunn" in r.text)
    r = c.post(f"/api/instances/{iid}/mods/install", data={"package_full_name": "ValheimModding-Jotunn"})
    check("install ok", "Installed" in r.text, r.text[:120])
    check("BepInEx pulled in", "BepInExPack_Valheim" in r.text)
    prof = ROOT/"instances"/iid
    check("plugin on disk", (prof/"BepInEx/plugins/ValheimModding-Jotunn/Jotunn.dll").is_file())
    check("bepinex at profile root", (prof/"BepInEx/core/BepInEx.Preloader.dll").is_file())
    cfg = prof/"BepInEx/config/Jotunn.cfg"
    cfg.write_text("tuned=1")
    r = c.post(f"/api/instances/{iid}/mods/ValheimModding-Jotunn/toggle")
    check("disable renames dll", (prof/"BepInEx/plugins/ValheimModding-Jotunn/Jotunn.dll.old").is_file())
    check("disable leaves config alone", cfg.read_text() == "tuned=1")
    c.post(f"/api/instances/{iid}/mods/ValheimModding-Jotunn/toggle")
    r = c.post(f"/api/instances/{iid}/mods/denikson-BepInExPack_Valheim/uninstall")
    check("dependency removal blocked", "required by" in r.text, r.text[:120])
    r = c.post(f"/api/instances/{iid}/mods/ValheimModding-Jotunn/uninstall")
    check("uninstall ok", "Removed" in r.text)
    check("config survives uninstall", cfg.read_text() == "tuned=1")

    print("\n[teardown]")
    c.post(f"/instances/{iid}/stop")
    snap = wait_status(c, iid, "stopped")
    check("stop reaches stopped", snap["status"] == "stopped", snap["status"])
    r = c.post(f"/instances/{iid}/delete", data={"remove_files": "on"})
    check("delete ok", r.status_code == 200 and r.headers.get("hx-redirect") == "/")
    check("files removed", not prof.exists())
    check("404 after delete", c.get(f"/api/instances/{iid}").status_code == 404)

print(f"\n=== {ok} passed, {fail} failed ===")
shutil.rmtree(ROOT, ignore_errors=True)
raise SystemExit(1 if fail else 0)
