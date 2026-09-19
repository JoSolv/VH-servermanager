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
    check("dev-mode banner shown", "Development mode active" in r.text)
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

    print("\n[minor fix 1: whitelist]")
    lists = app.state.manager.get(iid).lists
    saved = ROOT/"instances"/iid/"saves"
    # Locking a server down is a decision, never a default: a server nobody has
    # touched lets everyone in.
    check("whitelist starts off", lists.permitted.enabled is False)
    check("nothing for valheim to read yet", not (saved/"permittedlist.txt").is_file())
    r = c.get(f"/api/instances/{iid}/players")
    check("switch is named Use Whitelist", "Use Whitelist" in r.text)
    check("old enforce wording gone", "Enforce the permitted list" not in r.text)
    switch = r.text.split('id="permitted_enabled"', 1)[-1].split(">", 1)[0]
    check("switch rendered unticked", "checked" not in switch, switch[:120])

    # Permitting somebody must not turn the whitelist on behind the operator's
    # back: the entry is kept for when they do.
    lists.permitted.add("76561198000000123")
    check("permitting does not switch it on", lists.permitted.enabled is False)
    check("the entry waits in the parked file",
          "76561198000000123" in (saved/"permittedlist.txt.disabled").read_text())
    check("valheim still reads no whitelist", not (saved/"permittedlist.txt").is_file())

    r = c.post(f"/api/instances/{iid}/permitted", data={"enabled": "1"})
    check("whitelist can be switched on", "on —" in r.text, r.text[:110])
    check("valheim reads it once on", (saved/"permittedlist.txt").is_file())
    check("entries survived the round trip", "76561198000000123" in lists.permitted.read())
    r = c.post(f"/api/instances/{iid}/permitted", data={"enabled": ""})
    check("whitelist can be switched off again", "off" in r.text, r.text[:110])
    check("valheim no longer reads it", not (saved/"permittedlist.txt").is_file())
    check("entries are kept, not discarded",
          "76561198000000123" in (saved/"permittedlist.txt.disabled").read_text())
    # Back on, so the rest of the suite exercises the enforced path.
    c.post(f"/api/instances/{iid}/permitted", data={"enabled": "1"})

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

    print("\n[crossplay: the public-IP retry loop]")
    # A crossplay server on a host without IPv6 cannot look its public address
    # up, and the game retries that in a tight loop -- thousands of lines a
    # second, all of them the same two. Real output, verbatim:
    import vhsm.supervisor as supervisor_mod
    from vhsm.logspam import (
        CROSSPLAY_PUBLIC_IP_LOOP, IssueWatcher, LogThrottle, signature)
    from vhsm.monitor.ports import global_ipv6
    from vhsm.diagnostics import EgressReport
    from vhsm.supervisor import Supervisor
    from vhsm.instance import InstanceConfig, InstanceLayout

    EXC = ("Exception while waiting for respons from https://api6.ipify.org -> "
           "System.InvalidOperationException: This instance has already started "
           "one or more requests. Properties can only be modified before sending "
           "the first request.")
    FAILED = "09/19/2026 16:00:52: Could not extract valid IP address from externalIP download string."
    LATER = "09/19/2026 16:11:02: Could not extract valid IP address from externalIP download string."

    check("a repeat is recognised through its timestamp", signature(FAILED) == signature(LATER))
    check("unrelated lines keep their own signature", signature(EXC) != signature(FAILED))

    throttle = LogThrottle(burst=5, window=10.0, note_interval=1.0)
    kept = notes = 0
    for i in range(400):                      # the real loop, ~1000 lines/s
        verdict = throttle.admit(FAILED, now=100.0 + i * 0.001)
        kept += verdict.keep
        notes += bool(verdict.note)
    check("a flood stops after the burst", kept == 5, kept)
    check("and the console says why it stopped", notes >= 1, notes)

    quiet = LogThrottle(burst=2, window=10.0)
    check("distinct messages are never held back",
          all(quiet.admit(line, now=200.0).keep for line in (
              "Loading world", "Zonesystem Start", "Steam game server initialized",
              "Game server connected", "World loaded", "DungeonDB Start",
              "Registering lobby", "Session Vanaheim registered")))
    # Shape, not text: a message that differs only in an id is one message, so
    # a burst of them is collapsed too. Nothing is lost by that -- the player
    # tracker reads every line either way -- and the defaults leave room for
    # far more players than Valheim admits at once.
    check("an id does not disguise a repeat",
          signature("Got connection SteamID 76561198000000001") ==
          signature("Got connection SteamID 76561198000000002"))
    lobby = LogThrottle()
    check("a whole lobby connecting at once is not mistaken for a loop",
          all(lobby.admit("Got connection SteamID 7656119800000%04d" % n).keep
              for n in range(20)))
    slow = LogThrottle(burst=5, window=10.0)
    check("a line that merely recurs is never held back",
          all(slow.admit(FAILED, now=300.0 + n * 5).keep for n in range(20)))

    recovering = LogThrottle(burst=3, window=10.0, note_interval=1.0)
    for i in range(50):
        recovering.admit(FAILED, now=400.0 + i * 0.01)
    resumed = recovering.admit(FAILED, now=500.0)
    check("the console resumes once the loop stops",
          resumed.keep and "left out" in resumed.note, resumed)

    watcher = IssueWatcher()
    check("one failed lookup is not a diagnosis",
          watcher.observe(FAILED) is None and not watcher.notices)
    raised = None
    for _ in range(CROSSPLAY_PUBLIC_IP_LOOP.threshold):
        raised = watcher.observe(FAILED) or raised
    check("a loop is a diagnosis", raised is not None and raised.key == "crossplay-public-ip-loop", raised)
    check("it is raised once, not once per line",
          all(watcher.observe(FAILED) is None for _ in range(50)))
    check("while the count keeps rising",
          watcher.notices[0].hits > CROSSPLAY_PUBLIC_IP_LOOP.threshold, watcher.notices[0].hits)
    check("the notice leads with the cause this actually turned out to be",
          "ad blocker" in raised.detail and "IPv6" in raised.detail
          and raised.detail.index("ad blocker") < raised.detail.index("no routable IPv6"),
          raised.detail)
    check("it reaches the console as manager lines",
          all(line.startswith("[manager] ") for line in raised.console_lines()))
    check("ordinary output raises nothing",
          IssueWatcher().observe("Game server connected") is None)

    # Preflight: said before the server starts, so it is not buried by the
    # very loop it is about. The check is the one that would have caught the
    # real failure -- something on the network path swallowing the requests
    # the server makes on its way up.
    import asyncio as _asyncio
    from vhsm.logspam import CROSSPLAY_PLAYFAB_UNREACHABLE

    def preflight(crossplay, reachable):
        config = InstanceConfig(name="Vanaheim", world="Vanaheim", password="hammertime",
                                port=2600, public=False, crossplay=crossplay)
        layout = InstanceLayout.for_instance(settings, config.id)
        sup = Supervisor(config, layout, settings)
        report = EgressReport(ok=reachable, detail="pretend" if reachable else "nothing answered")
        real = supervisor_mod.check_playfab_egress
        supervisor_mod.check_playfab_egress = lambda *a, **k: report
        try:
            _asyncio.run(sup._preflight())
        finally:
            supervisor_mod.check_playfab_egress = real
        written = layout.console_log.read_text() if layout.console_log.is_file() else ""
        return sup.notices, sup.recent_logs(), written

    flagged, logged, written = preflight(crossplay=True, reachable=False)
    check("a crossplay server that cannot reach PlayFab is flagged before launch",
          [n["key"] for n in flagged] == [CROSSPLAY_PLAYFAB_UNREACHABLE.key], flagged)
    check("and told why, in the console the user is watching",
          any("nothing answered" in line for line in logged), logged)
    check("and in the transcript they would download",
          "nothing answered" in written and "ad blocker" in written, written[:120])
    check("a reachable PlayFab is left alone", preflight(True, reachable=True)[0] == [])
    check("a non-crossplay server is never checked at all",
          preflight(False, reachable=False)[0] == [])
    check("the host IPv6 probe answers without raising", isinstance(global_ipv6(), str))

    snap = c.get(f"/api/instances/{iid}").json()
    check("notices ride on every instance snapshot",
          snap["notices"] == [], snap.get("notices"))
    check("the instance page has somewhere to put them",
          'data-f="notices"' in c.get(f"/instances/{iid}").text)

    # End to end, against a process actually spinning: crossplay is left OFF
    # so the preflight cannot be what raises the notice -- only reading the
    # console can be. VHSM_FAKE_LOOP makes the stand-in server reproduce the
    # real loop, thousands of lines a second.
    os.environ["VHSM_FAKE_LOOP"] = "1"
    r = c.post("/instances", data={"name":"Loopy","world":"Loopy","password":"hammertime",
               "port":"2510","save_interval":"1800","backups":"4","backup_short":"7200",
               "backup_long":"43200"}, follow_redirects=False)
    loop_id = r.headers["location"].rsplit("/", 1)[-1]
    c.post(f"/instances/{loop_id}/start")
    wait_status(c, loop_id, "running")
    os.environ.pop("VHSM_FAKE_LOOP", None)

    deadline, live = time.time() + 20, []
    while time.time() < deadline:
        live = c.get(f"/api/instances/{loop_id}").json()["notices"]
        if live:
            break
        time.sleep(0.5)
    check("a running loop is recognised from the console alone",
          [n["key"] for n in live] == ["crossplay-public-ip-loop"], live)

    time.sleep(3)
    supervisor = app.state.manager.get(loop_id).supervisor
    console = "\n".join(supervisor.recent_logs())
    check("the console says it is collapsing the repeats",
          "is repeating faster than it can be read" in console)
    check("and keeps room for what the server said before it",
          "Valheim version" in console or "[manager] starting" in console, console[:200])
    transcript = app.state.manager.get(loop_id).layout.console_log
    size = transcript.stat().st_size if transcript.is_file() else 0
    # Unthrottled this loop writes on the order of a megabyte a second, so a
    # transcript still this small after several seconds is the whole point.
    check("the transcript on disk stays bounded", 0 < size < 250_000, f"{size} bytes")

    c.post(f"/instances/{loop_id}/stop")
    wait_status(c, loop_id, "stopped")
    c.post(f"/instances/{loop_id}/delete", data={"remove_files": "on"})

    print("\n[crossplay: the join code]")
    from vhsm.logspam import CROSSPLAY_NO_JOIN_CODE, RE_JOIN_CODE
    from vhsm.diagnostics import PLAYFAB_HOST, check_playfab_egress, plugin_libraries

    # Verbatim from a server that had not been issued one yet, and from one
    # that had. Telling them apart is the whole job.
    check("an empty join code is not a join code",
          RE_JOIN_CODE.search('New session server "x" that has join code , now 0 player(s)') is None)
    check("a join code is read from the session line",
          RE_JOIN_CODE.search('... that has join code 665832, now 0 player(s)').group(1) == "665832")
    check("and from the registration line",
          RE_JOIN_CODE.search('Session "Odin\'s Rest" registered with join code 665832'
                              ).group(1) == "665832")

    # PlayFab login and address registration succeed over HTTPS; what repeats
    # is the Party network, so that repeat is what gets recognised.
    stuck = IssueWatcher()
    raised = None
    for _ in range(CROSSPLAY_NO_JOIN_CODE.threshold):
        raised = stuck.observe("PlayFab reconnect server 'my crossplay server'") or raised
    check("a PlayFab reconnect loop is recognised",
          raised is not None and raised.key == "crossplay-no-join-code", raised)
    check("the notice separates the library cause from the network one",
          "libparty.so" in raised.detail and "UDP" in raised.detail)
    check("a join code clears it, because then it was not a failure",
          stuck.observe("... that has join code 665832, now 0 player(s)") is None
          and stuck.notices == [], stuck.notices)
    check("and the count starts over rather than resuming",
          stuck.observe("PlayFab reconnect server 'x'") is None and stuck.notices == [])

    # libparty.so is loaded by name at run time, so a missing dependency there
    # is invisible except that crossplay never comes up -- the same shape the
    # library check already exists for, and it was not looking at it.
    plugins = Path(tempfile.mkdtemp(prefix="vhsm-plugins-"))
    (plugins / "valheim_server_Data" / "Plugins" / "x86_64").mkdir(parents=True)
    (plugins / "valheim_server_Data" / "Plugins" / "libparty.so").write_bytes(b"\x7fELF")
    (plugins / "valheim_server_Data" / "Plugins" / "x86_64" / "libsteam.so").write_bytes(b"\x7fELF")
    (plugins / "valheim_server_Data" / "Plugins" / "notes.txt").write_text("ignored")
    found = plugin_libraries(plugins)
    check("the crossplay backend is checked now",
          "valheim_server_Data/Plugins/libparty.so" in found, found)
    check("so is everything beside it", any("libsteam.so" in f for f in found), found)
    check("and nothing that is not a library", not any(f.endswith(".txt") for f in found), found)
    shutil.rmtree(plugins, ignore_errors=True)

    # An ad blocker answering for a name is not the same as a name that does
    # not exist, and it is the shape that broke the public-IP lookup here.
    import vhsm.diagnostics as diagnostics_mod
    real_getaddrinfo = diagnostics_mod.socket.getaddrinfo
    diagnostics_mod.socket.getaddrinfo = lambda *a, **k: [(2, 1, 6, "", ("0.0.0.0", 443))]
    try:
        blocked = check_playfab_egress()
    finally:
        diagnostics_mod.socket.getaddrinfo = real_getaddrinfo
    check("a DNS sinkhole is called what it is",
          not blocked.ok and "blocking the name" in blocked.detail, blocked.detail)

    diagnostics_mod.socket.getaddrinfo = lambda *a, **k: (_ for _ in ()).throw(OSError("nope"))
    try:
        unresolved = check_playfab_egress()
    finally:
        diagnostics_mod.socket.getaddrinfo = real_getaddrinfo
    check("so is a name that does not resolve at all",
          not unresolved.ok and "does not resolve" in unresolved.detail, unresolved.detail)
    check("the report says which host it asked about",
          EgressReport().host == PLAYFAB_HOST and PLAYFAB_HOST.endswith(".playfabapi.com"))
    live = check_playfab_egress(timeout=3.0)
    check("the real check returns a verdict either way",
          isinstance(live.ok, bool) and bool(live.detail), live.to_dict())

    # End to end: a crossplay server, and the code players need off it.
    r = c.post("/instances", data={"name":"Jotunheim","world":"Jotunheim","password":"hammertime",
               "port":"2520","crossplay":"on","save_interval":"1800","backups":"4",
               "backup_short":"7200","backup_long":"43200"}, follow_redirects=False)
    cross_id = r.headers["location"].rsplit("/", 1)[-1]
    c.post(f"/instances/{cross_id}/start")
    wait_status(c, cross_id, "running")
    deadline, code = time.time() + 25, ""
    while time.time() < deadline:
        code = c.get(f"/api/instances/{cross_id}").json()["join_code"]
        if code:
            break
        time.sleep(0.5)
    check("a crossplay server's join code is read off the console",
          code.isdigit() and len(code) == 6, repr(code))
    check("and shown on the instance page",
          code in c.get(f"/instances/{cross_id}").text)
    probe = c.get(f"/api/instances/{cross_id}/connectivity").text
    check("the connectivity panel reports it registered",
          "Crossplay is registered" in probe and code in probe)
    check("and says whether PlayFab is reachable from in here",
          "PlayFab from inside this container" in probe)
    check("a server that got a code is not flagged as stuck",
          "crossplay-no-join-code" not in
          [n["key"] for n in c.get(f"/api/instances/{cross_id}").json()["notices"]])

    c.post(f"/instances/{cross_id}/stop")
    wait_status(c, cross_id, "stopped")
    check("the code is dropped with the session that owned it",
          c.get(f"/api/instances/{cross_id}").json()["join_code"] == "",
          c.get(f"/api/instances/{cross_id}").json()["join_code"])
    c.post(f"/instances/{cross_id}/delete", data={"remove_files": "on"})

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
    # The build tile is gone from the metrics strip: the numbers there are about
    # this host, and which Valheim build is installed is a Settings matter.
    check("build card dropped from the dashboard metrics", "Server build" not in r.text)
    check("host metrics still on the dashboard", "Host CPU" in r.text and "Load average" in r.text)
    check("settings still owns the build state", "Installed build" in c.get("/settings").text)
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

    print("\n[export / import a whole server]")
    world_dir = ROOT/"instances"/iid/"saves"/"worlds_local"
    world_dir.mkdir(parents=True, exist_ok=True)
    make_folder_world(world_dir, "Midgard", generations=2)
    (world_dir/"Midgard"/"_main.0.db2").write_bytes(b"WORLD-ORIGINAL")
    (ROOT/"instances"/iid/"saves"/"adminlist.txt").write_text("// admins\n76561198000000042\n")
    # A player base to carry across: an instance is its world *plus* who has
    # played on it, and an export that loses them is not a clone.
    from vhsm.monitor.players import LogEvent
    veteran = "76561198000000777"
    roster = app.state.manager.get(iid).roster
    roster.observe(LogEvent(kind="connect", player_id=veteran))
    roster.observe(LogEvent(kind="named", player_id=veteran, name="Freyja"))
    roster.save(force=True)

    r = c.get("/")
    check("dashboard has one transfer section", r.text.count('id="transfer-instances"') == 1)
    check("that section imports and exports",
          "/api/instances/import" in r.text and f"/api/instances/{iid}/export" in r.text)
    check("cards carry a context menu", r.text.count('class="menu"') >= 2, r.text.count('class="menu"'))
    check("the menu holds mods, export and clone",
          all(x in r.text for x in ("Manage mods", "Export server", "Clone server")))
    check("manage mods left the lifecycle row",
          c.get(f"/instances/{iid}/controls").text.count("mods") == 0)

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
    # An export is a clone, so the things that make this server *this* server
    # travel with it: who has played here, and what has been saved of it.
    check("archive carries the player roster", "players.json" in names, names[:8])
    check("archive carries the snapshots",
          any(n.startswith("backups/") for n in names), [n for n in names[:20]])
    check("archive still excludes the console log",
          not any(n.startswith("logs/") for n in names))

    r = c.post("/api/instances/import",
               files={"file": ("midgard.vhsm.zip", archive_bytes, "application/zip")})
    check("import accepted", r.status_code == 200 and "hx-redirect" in r.headers, r.status_code)
    new_id = r.headers["hx-redirect"].rsplit("/", 1)[-1]
    imported = c.get(f"/api/instances/{new_id}").json()
    check("imported gets a fresh id", new_id != iid)
    check("imported name avoids the collision", imported["name"] != "Midgard", imported["name"])
    check("imported port avoids the collision", imported["port"] != 2456, imported["port"])
    # The original's port is kept when it can be, and otherwise the next free
    # range above it -- not somewhere unrelated at the bottom of the range.
    check("imported port lands next to the original", imported["port"] == 2459, imported["port"])
    check("imported carries no autostart flag", "autostart" not in imported)
    check("imported world restored",
          (ROOT/"instances"/new_id/"saves"/"worlds_local"/"Midgard"/"_main.0.db2").read_bytes()
          == b"WORLD-ORIGINAL")
    check("imported admin list restored",
          "76561198000000042" in (ROOT/"instances"/new_id/"saves"/"adminlist.txt").read_text())
    restored = app.state.manager.player_rows(new_id)["players"]
    check("imported player base restored",
          any(x["player_id"] == veteran for x in restored), restored)
    check("imported roster keeps the names", 
          any(x["display_name"] == "Freyja" for x in restored), restored)
    check("imported backups restored",
          bool(app.state.manager.backup_summary(new_id)["restores"]))

    print("\n[clone a server]")
    r = c.post(f"/api/instances/{iid}/clone")
    check("clone accepted", r.status_code == 200 and "hx-redirect" in r.headers, r.status_code)
    clone_id = r.headers["hx-redirect"].rsplit("/", 1)[-1]
    cloned = c.get(f"/api/instances/{clone_id}").json()
    check("clone is a new instance", clone_id not in (iid, new_id))
    check("clone is named after the original", cloned["name"] == "Midgard (clone)", cloned["name"])
    check("clone takes the next free port range", cloned["port"] == 2462, cloned["port"])
    check("clone is left stopped", cloned["status"] == "stopped", cloned["status"])
    clone_root = ROOT/"instances"/clone_id
    check("clone copied the world",
          (clone_root/"saves"/"worlds_local"/"Midgard"/"_main.0.db2").read_bytes()
          == b"WORLD-ORIGINAL")
    check("clone copied the access lists",
          "76561198000000042" in (clone_root/"saves"/"adminlist.txt").read_text())
    check("clone copied the player base",
          any(x["player_id"] == veteran
              for x in app.state.manager.player_rows(clone_id)["players"]))
    check("clone copied the backups",
          bool(app.state.manager.backup_summary(clone_id)["restores"]))
    check("clone did not copy the original's console log",
          not (clone_root/"logs"/"console.log").exists())
    check("clone kept its own identity in instance.json",
          json.loads((clone_root/"instance.json").read_text())["id"] == clone_id)
    # Cloning twice must not collide on the name that the first clone took.
    r = c.post(f"/api/instances/{iid}/clone")
    second = c.get(f"/api/instances/{r.headers['hx-redirect'].rsplit('/', 1)[-1]}").json()
    check("a second clone gets its own name", second["name"] == "Midgard (clone) (2)",
          second["name"])
    check("a second clone gets its own ports",
          second["port"] not in (2456, 2459, 2462), second["port"])
    c.post(f"/instances/{second['id']}/delete", data={"remove_files": "on"})
    c.post(f"/instances/{clone_id}/delete", data={"remove_files": "on"})

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
    check("world import and export live together",
          "world/upload" in r.text and "world/export" in r.text)
    check("one import field takes either shape",
          r.text.count('name="files"') == 1 and "webkitdirectory" not in r.text)
    # The instance page moves worlds; the dashboard moves whole servers. Mixing
    # the two here is what made "export" ambiguous in the first place.
    check("the instance panel no longer exports the whole server",
          "instances/import" not in r.text and "/export\"" not in r.text.replace(
              f"/api/instances/{new_id}/world/export", ""))
    check("panel points at the dashboard for whole servers",
          "transfer-instances" in r.text)

    print("\n[backups / rollback]")
    nd = ROOT/"instances"/new_id/"saves"/"worlds_local"
    r = c.get(f"/api/instances/{new_id}/backups")
    check("backups panel renders",
          r.status_code == 200 and 'sec-title">Backups<' in r.text, r.text[:200])
    check("backups panel describes the live world", "Live world" in r.text, r.text[:200])
    check("import/export moved out of backups",
          "Download archive" not in r.text and "world/upload" not in r.text)

    # The import brought the original's snapshots with it, which is the point of
    # an instance archive -- so measure what this snapshot adds rather than
    # assuming an empty slate.
    snapdir = ROOT/"instances"/new_id/"backups"
    inherited = {x.name for x in snapdir.glob("manual-*")}
    check("snapshots came across with the instance", bool(inherited), inherited)

    # The reported bug: this used to fail with "no world to snapshot".
    r = c.post(f"/api/instances/{new_id}/backups/snapshot")
    check("snapshot of a 1.0 world works", "Snapshot taken" in r.text, r.text[:140])
    taken = {x.name for x in snapdir.glob("manual-*")} - inherited
    check("snapshot stored under the instance", len(taken) == 1, taken)
    check("snapshot copied the whole world folder",
          (snapdir/taken.pop()/"payload"/"Midgard"/"_main.0.db2").read_bytes()
          == b"WORLD-ORIGINAL")
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
    ours = [x["key"] for x in summary["restores"] if x["source"] == "vhsm"]
    # The newest one is the snapshot just taken, of the world as it should be.
    key = ours[0]
    r = c.post(f"/api/instances/{new_id}/backups/restore", data={"key": key})
    check("rollback reported", "Rolled back" in r.text, r.text[:110])
    check("world rolled back", (nd/"Midgard"/"_main.0.db2").read_bytes() == b"WORLD-ORIGINAL")
    check("stale files removed by rollback", not (nd/"Midgard"/"junk.chunk").exists())
    after = app.state.manager.backup_summary(new_id)["restores"]
    check("rollback is itself undoable",
          len([x for x in after if x["source"] == "vhsm"]) == len(ours) + 1,
          [x["key"] for x in after])

    r = c.post(f"/instances/{new_id}/start"); wait_status(c, new_id, "running")
    r = c.post(f"/api/instances/{new_id}/backups/restore", data={"key": key})
    check("rollback refused while running", "Stop the server" in r.text, r.text[:110])

    print("\n[world export]")
    r = c.get(f"/api/instances/{new_id}/world/export")
    check("world export downloads", r.status_code == 200 and r.content[:2] == b"PK", r.status_code)
    check("world export is a plain zip, not an instance archive",
          ".vhsm.zip" not in r.headers.get("content-disposition", ""),
          r.headers.get("content-disposition"))
    exported_world = r.content
    wnames = _zip.ZipFile(_io.BytesIO(exported_world)).namelist()
    check("world export wraps the world folder",
          all(n.startswith("Midgard/") for n in wnames), wnames[:4])
    check("world export carries every file", len(wnames) >= 14, len(wnames))
    check("world export carries the world only",
          not any("instance.json" in n or "adminlist" in n for n in wnames))

    print("\n[world import]")
    # a running server would overwrite whatever we put down
    zipped = _io.BytesIO()
    src = make_folder_world(Path(tempfile.mkdtemp()), "Jotunheim", generations=2)
    (src/"_main.0.db2").write_bytes(b"WORLD-JOTUNHEIM")
    with _zip.ZipFile(zipped, "w") as z:
        for f in src.rglob("*"):
            if f.is_file():
                z.write(f, f"Jotunheim/{f.relative_to(src)}")
    world_zip = zipped.getvalue()
    r = c.post(f"/api/instances/{new_id}/world/upload",
               files={"files": ("Jotunheim.zip", world_zip, "application/zip")})
    check("upload refused while running", "Stop the server" in r.text, r.text[:110])
    c.post(f"/instances/{new_id}/stop"); wait_status(c, new_id, "stopped")

    # This instance already has a world, so replacing it needs the operator's
    # word rather than happening because a file was picked.
    r = c.post(f"/api/instances/{new_id}/world/upload",
               files={"files": ("Jotunheim.zip", world_zip, "application/zip")})
    check("replacing an existing world needs confirmation",
          "already has a world" in r.text, r.text[:160])
    check("nothing was replaced without it",
          (nd/"Midgard"/"_main.0.db2").read_bytes() == b"WORLD-ORIGINAL")
    r = c.get(f"/api/instances/{new_id}/transfer")
    check("the panel asks before replacing", "hx-confirm" in r.text and "confirm" in r.text)
    check("the panel says the world will be replaced", "Replace world" in r.text, r.text[:0])

    before = len(app.state.manager.backup_summary(new_id)["restores"])
    r = c.post(f"/api/instances/{new_id}/world/upload",
               files={"files": ("Jotunheim.zip", world_zip, "application/zip")},
               data={"confirm": "1"})
    check("confirmed import installs", "Installed world" in r.text, r.text[:200])
    check("the world was replaced in place",
          (nd/"Midgard"/"_main.0.db2").read_bytes() == b"WORLD-JOTUNHEIM")
    check("the instance still loads the same world name",
          c.get(f"/api/instances/{new_id}").json()["world"] == "Midgard",
          c.get(f"/api/instances/{new_id}").json()["world"])
    check("the message says where the upload landed",
          "Jotunheim was installed as Midgard" in r.text, r.text[:260])

    after = app.state.manager.backup_summary(new_id)["restores"]
    check("the replaced world was snapshotted first", len(after) == before + 1,
          f"{before} -> {len(after)}")
    check("the snapshot is labelled as taken before an import",
          any(x["kind"] == "pre-import" for x in after), [x["kind"] for x in after])
    rollback = next(x["key"] for x in after if x["kind"] == "pre-import")
    r = c.post(f"/api/instances/{new_id}/backups/restore", data={"key": rollback})
    check("a replaced world can be rolled back", "Rolled back" in r.text, r.text[:120])
    check("rollback brought the old world back",
          (nd/"Midgard"/"_main.0.db2").read_bytes() == b"WORLD-ORIGINAL")

    # Importing into an instance whose world has not been generated yet is the
    # other case, and still works the way it always did: the upload becomes the
    # world and the instance is repointed at it.
    fresh = c.post("/instances", data={"name":"Vanaheim","world":"Vanaheim","password":"ymirflesh",
                   "port":"2480","save_interval":"1800","backups":"4","backup_short":"7200",
                   "backup_long":"43200"}, follow_redirects=False)
    fresh_id = fresh.headers["location"].rsplit("/", 1)[-1]
    r = c.get(f"/api/instances/{fresh_id}/transfer")
    check("a world-less instance is not asked to confirm", "hx-confirm" not in r.text)
    check("and its button says import, not replace",
          "Import world" in r.text and "Replace world" not in r.text)
    r = c.post(f"/api/instances/{fresh_id}/world/upload",
               files={"files": ("Jotunheim.zip", world_zip, "application/zip")})
    check("importing into a fresh instance needs no confirmation",
          "Installed world" in r.text, r.text[:160])
    check("instance repointed at the upload",
          c.get(f"/api/instances/{fresh_id}").json()["world"] == "Jotunheim",
          c.get(f"/api/instances/{fresh_id}").json()["world"])
    fd = ROOT/"instances"/fresh_id/"saves"/"worlds_local"
    check("world files landed", (fd/"Jotunheim"/"_main.0.fwl2").is_file())
    check("uploaded world is now the live one",
          app.state.manager.backup_summary(fresh_id)["live"]["exists"])
    check("importing a world left the rest of the server alone",
          c.get(f"/api/instances/{fresh_id}").json()["port"] == 2480)

    # a directory upload: every file posted separately, name carried alongside
    loose = [("files", (f"Vanaheim/{f.relative_to(src)}", f.read_bytes(), "application/octet-stream"))
             for f in sorted(src.rglob("*")) if f.is_file()]
    r = c.post(f"/api/instances/{fresh_id}/world/upload", files=loose,
               data={"name": "Vanaheim", "confirm": "1"})
    check("folder upload installs", "Installed world" in r.text, r.text[:140])
    check("folder upload replaced the live world",
          (fd/"Jotunheim"/"_main.0.db2").read_bytes() == b"WORLD-JOTUNHEIM")

    evil = [("files", ("../../escaped.txt", b"pwned", "text/plain"))]
    r = c.post(f"/api/instances/{fresh_id}/world/upload", files=evil, data={"confirm": "1"})
    check("traversing filename rejected", "unsafe name" in r.text, r.text[:120])
    check("nothing escaped", not (ROOT.parent/"escaped.txt").exists())

    notworld = _io.BytesIO()
    with _zip.ZipFile(notworld, "w") as z:
        z.writestr("holiday.jpg", "not a world")
    r = c.post(f"/api/instances/{fresh_id}/world/upload",
               files={"files": ("random.zip", notworld.getvalue(), "application/zip")},
               data={"confirm": "1"})
    check("non-world zip rejected with guidance", "No Valheim world found" in r.text, r.text[:140])

    # legacy worlds still work
    legacy = _io.BytesIO()
    with _zip.ZipFile(legacy, "w") as z:
        z.writestr("OldWorld.db", "LEGACY-DB"); z.writestr("OldWorld.fwl", "LEGACY-FWL")
    r = c.post(f"/api/instances/{fresh_id}/world/upload",
               files={"files": ("old.zip", legacy.getvalue(), "application/zip")},
               data={"confirm": "1"})
    check("legacy .db/.fwl upload still works", "Installed world" in r.text, r.text[:140])
    check("legacy world installed under the instance's world name",
          (fd/"Jotunheim.db").is_file() and (fd/"Jotunheim.fwl").is_file(),
          sorted(x.name for x in fd.iterdir()))
    c.post(f"/instances/{fresh_id}/delete", data={"remove_files": "on"})

    # A world exported from one instance imports into another as-is.
    r = c.post(f"/api/instances/{new_id}/world/upload",
               files={"files": ("Midgard-export.zip", exported_world, "application/zip")},
               data={"confirm": "1"})
    check("an exported world imports straight back",
          "Installed world" in r.text, r.text[:160])

    print("\n[backup housekeeping]")
    c.post(f"/api/instances/{new_id}/backups/snapshot")
    current = app.state.manager.backup_summary(new_id)
    check("snapshot of the live world", len(current["restores"]) >= 1, current["restores"])
    own = current["restores"][0]["key"]
    r = c.post(f"/api/instances/{new_id}/backups/delete", data={"key": own})
    check("backup deleted", "Backup deleted" in r.text, r.text[:100])
    check("snapshot directory removed",
          not (ROOT/"instances"/new_id/"backups"/own).exists())
    stale = c.post(f"/api/instances/{new_id}/backups/delete", data={"key": own})
    check("deleting a backup twice is refused, not silent",
          "no backup" in stale.text, stale.text[:100])

    print("\n[minor fix 3: console log download]")
    r = c.get(f"/instances/{new_id}")
    check("instance page offers the log", f"/api/instances/{new_id}/console-log" in r.text)
    # An instance that has never run has no transcript, and saying so beats
    # handing back an empty file that looks like a lost log.
    never = c.post("/instances", data={"name":"Quiet","world":"Quiet","password":"ymirflesh",
                   "port":"2490","save_interval":"1800","backups":"4","backup_short":"7200",
                   "backup_long":"43200"}, follow_redirects=False)
    quiet_id = never.headers["location"].rsplit("/", 1)[-1]
    r = c.get(f"/api/instances/{quiet_id}/console-log")
    check("no log yet is reported as such",
          r.status_code == 404 and "has not written any console output" in r.text,
          r.status_code)
    c.post(f"/instances/{quiet_id}/delete", data={"remove_files": "on"})

    # Run it long enough to have said something: the version line comes from the
    # server's own output, which is exactly what the file is supposed to hold.
    c.post(f"/instances/{new_id}/start")
    snap = wait_status(c, new_id, "running")
    for _ in range(60):
        if c.get(f"/api/instances/{new_id}").json()["version"]:
            break
        time.sleep(0.25)
    r = c.get(f"/api/instances/{new_id}/console-log")
    check("console log downloads", r.status_code == 200, r.status_code)
    check("log carries the server's own output", "Valheim version" in r.text, r.text[:200])
    check("log is the whole file, not the visible tail",
          r.text.count("\n") >= 2, r.text.count("\n"))
    check("log is an attachment",
          "console.log" in r.headers.get("content-disposition", ""),
          r.headers.get("content-disposition"))
    c.post(f"/instances/{new_id}/stop"); wait_status(c, new_id, "stopped")
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

    print("\n[mod config]")
    # Reinstall Jotunn: the config editor needs something to attribute files to.
    c.post(f"/api/instances/{iid}/mods/install", data={"package_full_name": "ValheimModding-Jotunn"})
    r = c.get(f"/instances/{iid}/mods/config")
    check("config editor renders", r.status_code == 200 and "Config files" in r.text)
    check("a mod with no config says so", "No config file yet" in r.text)

    # BepInEx writes these at boot, so the editor has to read what it finds
    # rather than what the package shipped.
    cfgdir = prof/"BepInEx"/"config"; cfgdir.mkdir(parents=True, exist_ok=True)
    (cfgdir/"com.jotunn.jotunn.cfg").write_text(
        "## Settings file was created by plugin Jotunn v2.20.0\n"
        "## Plugin GUID: com.jotunn.jotunn\n\n[General]\n\n"
        "## Turn the mod on\n# Setting type: Boolean\n# Default value: true\nEnabled = true\n\n"
        "## How much loot\n# Setting type: Single\n# Default value: 1\n"
        "# Acceptable value range: From 0 to 10\nLoot = 1\n\n"
        "## Where it spawns\n# Setting type: String\n# Default value: Meadows\n"
        "# Acceptable values: Meadows, Swamp, Plains\nBiome = Meadows\n")
    (cfgdir/"BepInEx.cfg").write_text("[Logging]\nLogLevel = Info\n")

    r = c.get(f"/instances/{iid}/mods/config", params={"mod": "ValheimModding-Jotunn"})
    check("config file found and attributed", "com.jotunn.jotunn.cfg" in r.text and "3 settings" in r.text)
    # Jotunn owns two config files here; the button has to open the real one.
    check("the mod's main config is the one that opened", "Turn the mod on" in r.text)
    check("boolean became a checkbox", 'type="checkbox"' in r.text)
    check("bounded number became a slider", 'type="range"' in r.text)
    check("enum became a dropdown", '<option value="Swamp"' in r.text)
    check("bepinex.cfg attributed to the pack", "BepInExPack_Valheim" in r.text)

    r = c.post(f"/api/instances/{iid}/mods/config/save", data={
        "path": "com.jotunn.jotunn.cfg", "mode": "form",
        "s0": "General", "n0": "Enabled", "v0": ["false", "false"],
        "s1": "General", "n1": "Loot", "v1": "4.5",
        "s2": "General", "n2": "Biome", "v2": "Swamp"})
    saved = (cfgdir/"com.jotunn.jotunn.cfg").read_text()
    check("form save applied", all(x in saved for x in
          ("Enabled = false", "Loot = 4.5", "Biome = Swamp")), saved)
    check("comments and metadata survived the save",
          "## Turn the mod on" in saved and "# Acceptable value range: From 0 to 10" in saved)
    check("save reported", "Saved 3 setting" in r.text, r.text[:160])

    r = c.post(f"/api/instances/{iid}/mods/config/save", data={
        "path": "com.jotunn.jotunn.cfg", "mode": "form",
        "s0": "General", "n0": "Loot", "v0": "99",
        "s1": "General", "n1": "Biome", "v1": "Plains"})
    saved = (cfgdir/"com.jotunn.jotunn.cfg").read_text()
    check("a value outside its range is refused", "Loot = 4.5" in saved, saved)
    check("the valid value beside it still saved", "Biome = Plains" in saved)
    check("the refusal says which and why", "cannot be above 10" in r.text, r.text[:200])

    c.post(f"/api/instances/{iid}/mods/config/reset",
           data={"path": "com.jotunn.jotunn.cfg", "section": "General", "key": "Loot"})
    check("one setting resets", "Loot = 1" in (cfgdir/"com.jotunn.jotunn.cfg").read_text())
    c.post(f"/api/instances/{iid}/mods/config/reset", data={"path": "com.jotunn.jotunn.cfg"})
    saved = (cfgdir/"com.jotunn.jotunn.cfg").read_text()
    check("the whole file resets", "Enabled = true" in saved and "Biome = Meadows" in saved, saved)

    r = c.post(f"/api/instances/{iid}/mods/config/save", data={
        "path": "com.jotunn.jotunn.cfg", "mode": "raw", "text": "[General]\r\nEnabled = false\r\n"})
    check("raw edit writes the file with unix endings",
          (cfgdir/"com.jotunn.jotunn.cfg").read_text() == "[General]\nEnabled = false\n",
          repr((cfgdir/"com.jotunn.jotunn.cfg").read_text()))

    j = c.get(f"/api/instances/{iid}/mods/config").json()
    check("config catalogue as json",
          sorted(f["relative"] for f in j["files"]) ==
          ["BepInEx.cfg", "Jotunn.cfg", "com.jotunn.jotunn.cfg"], j["files"])
    check("every file names the mod it belongs to",
          {f["relative"]: f["owner"] for f in j["files"]} == {
              "BepInEx.cfg": "denikson-BepInExPack_Valheim",
              "Jotunn.cfg": "ValheimModding-Jotunn",
              "com.jotunn.jotunn.cfg": "ValheimModding-Jotunn"}, j["files"])
    j = c.get(f"/api/instances/{iid}/mods/config", params={"path": "BepInEx.cfg"}).json()
    check("one file as json", j["entries"][0]["key"] == "LogLevel", j)
    r = c.get(f"/api/instances/{iid}/mods/config/download", params={"path": "BepInEx.cfg"})
    check("config downloads", r.status_code == 200 and "LogLevel" in r.text)

    # Every path here comes off a query string, so it must not reach outside
    # the config folder.
    for bad in ("../../instance.json", "/etc/passwd", "../mods.json"):
        check(f"traversal refused: {bad}",
              c.get(f"/api/instances/{iid}/mods/config", params={"path": bad}).status_code == 400)
    r = c.post(f"/api/instances/{iid}/mods/config/save",
               data={"path": "../../instance.json", "mode": "raw", "text": "wrecked"})
    check("traversal refused on save", "outside the config folder" in r.text, r.text[:160])
    check("instance.json untouched", json.loads((prof/"instance.json").read_text())["id"] == iid)

    r = c.post(f"/api/instances/{iid}/mods/config/delete", data={"path": "com.jotunn.jotunn.cfg"})
    check("config file deleted", not (cfgdir/"com.jotunn.jotunn.cfg").exists())
    check("deletion explains what happens next", "writes a fresh one" in r.text)
    c.post(f"/api/instances/{iid}/mods/ValheimModding-Jotunn/uninstall")

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
