"""End-to-end smoke test: pages, lifecycle, live metrics, and the mod flow.

Runs entirely against the simulated server, so it needs no Steam download and
no network access. Usage:  python tests/smoke_test.py
"""

import json, os, shutil, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["VHSM_FAKE_SERVER"] = "1"
ROOT = Path(tempfile.mkdtemp(prefix="vhsm-smoke-"))
os.environ["VHSM_DATA_ROOT"] = str(ROOT)

from fastapi.testclient import TestClient
from vhsm.config import Settings
from vhsm.web.app import create_app
from vhsm.mods.thunderstore import Package
from vhsm.mods.cache import COMPLETE_MARKER

settings = Settings(); settings.ensure_dirs()
app = create_app(settings)

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

ok = fail = 0
def check(label, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  PASS  {label}")
    else:    fail += 1; print(f"  FAIL  {label} {extra}")

with TestClient(app) as c:
    idx = app.state.manager.index
    seed(idx, "denikson", "BepInExPack_Valheim", "5.4.2202", {
        "manifest.json": "{}",
        "BepInExPack_Valheim/BepInEx/core/BepInEx.Preloader.dll": "MZ",
        "BepInExPack_Valheim/doorstop_libs/libdoorstop_x64.so": "ELF"})
    seed(idx, "ValheimModding", "Jotunn", "2.20.0",
         {"manifest.json": "{}", "plugins/Jotunn.dll": "MZ"},
         deps=["denikson-BepInExPack_Valheim-5.4.2202"])
    idx._fetched_at = time.time()

    print("\n[pages]")
    r = c.get("/"); check("GET / renders", r.status_code == 200 and "Instances" in r.text)
    check("dev-mode banner shown", "development mode" in r.text)
    r = c.get("/instances/new"); check("GET /instances/new", r.status_code == 200 and 'name="world"' in r.text)
    r = c.get("/settings"); check("GET /settings", r.status_code == 200 and "Dedicated server files" in r.text)

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
    check("modifier persisted", 'value="hard" selected' in r.text or "hard" in r.text)

    print("\n[lifecycle]")
    r = c.post(f"/instances/{iid}/start"); check("start returns controls", r.status_code == 200 and "Stop" in r.text)
    snap = c.get(f"/api/instances/{iid}").json()
    check("status running", snap["status"] == "running", snap["status"])
    check("pid assigned", bool(snap["pid"]))
    r = c.post(f"/instances/{iid}/start")
    check("double start reports error", "already" in r.text.lower(), r.text[:80])

    time.sleep(4)
    with c.websocket_connect("/ws/metrics") as ws:
        payload = ws.receive_json()
        check("ws snapshot type", payload["type"] == "snapshot")
        check("ws has host metrics", "cpu_percent" in payload["host"])
        inst = [i for i in payload["instances"] if i["id"] == iid][0]
        check("ws cpu present", isinstance(inst["metrics"]["cpu_percent"], float))
        check("ws rss > 0", inst["metrics"]["memory_rss"] > 0, inst["metrics"]["memory_rss"])
        check("ws net source nftables", inst["net"]["source"] == "nftables", inst["net"]["source"])
        check("per-instance net available", payload["net_per_instance"]["available"])

    with c.websocket_connect(f"/ws/console/{iid}") as ws:
        line = ws.receive_text()
        check("console streams", "[manager]" in line or "Valheim" in line, line)

    logs = c.get(f"/api/instances/{iid}/logs").json()["lines"]
    check("log buffer populated", any("Valheim version" in l for l in logs))

    print("\n[mods]")
    r = c.get(f"/instances/{iid}/mods"); check("mods page renders", r.status_code == 200 and "Thunderstore" in r.text)
    r = c.get("/api/mods/search", params={"instance_id": iid, "q": "jotunn"})
    check("search finds Jotunn", r.status_code == 200 and "Jotunn" in r.text)
    r = c.post(f"/api/instances/{iid}/mods/install", data={"package_full_name": "ValheimModding-Jotunn"})
    check("install ok", "Installed" in r.text, r.text[:120])
    check("BepInEx pulled in", "BepInExPack_Valheim" in r.text)
    check("dependency tagged", "dependency" in r.text)
    prof = ROOT/"instances"/iid
    check("plugin on disk", (prof/"BepInEx/plugins/ValheimModding-Jotunn/Jotunn.dll").is_file())
    check("bepinex at profile root", (prof/"BepInEx/core/BepInEx.Preloader.dll").is_file())
    check("mods_enabled auto-set", c.get(f"/api/instances/{iid}").json()["mods_enabled"])

    r = c.post(f"/api/instances/{iid}/mods/ValheimModding-Jotunn/toggle")
    check("disable ok", "disabled" in r.text)
    check("dll renamed .old", (prof/"BepInEx/plugins/ValheimModding-Jotunn/Jotunn.dll.old").is_file())
    c.post(f"/api/instances/{iid}/mods/ValheimModding-Jotunn/toggle")
    check("re-enable restores dll", (prof/"BepInEx/plugins/ValheimModding-Jotunn/Jotunn.dll").is_file())

    r = c.post(f"/api/instances/{iid}/mods/denikson-BepInExPack_Valheim/uninstall")
    check("dependency removal blocked", "required by" in r.text, r.text[:120])

    exp = c.get(f"/api/instances/{iid}/mods/export")
    check("export works", exp.status_code == 200 and len(exp.json()["mods"]) == 2)
    check("export filename", "midgard" in exp.headers.get("content-disposition", ""))

    r = c.post(f"/api/instances/{iid}/mods/ValheimModding-Jotunn/uninstall")
    check("uninstall ok", "Removed" in r.text)
    check("plugin dir gone", not (prof/"BepInEx/plugins/ValheimModding-Jotunn").exists())

    r = c.post(f"/api/instances/{iid}/mods/prune")
    check("prune keeps BepInEx", "No unused" in r.text or "BepInEx" not in r.text)

    print("\n[teardown]")
    r = c.post(f"/instances/{iid}/stop"); check("stop ok", r.status_code == 200)
    check("status stopped", c.get(f"/api/instances/{iid}").json()["status"] == "stopped")
    r = c.post(f"/instances/{iid}/delete", data={"remove_files": "on"})
    check("delete ok", r.status_code == 200 and r.headers.get("hx-redirect") == "/")
    check("files removed", not prof.exists())
    check("404 after delete", c.get(f"/api/instances/{iid}").status_code == 404)

print(f"\n=== {ok} passed, {fail} failed ===")
shutil.rmtree(ROOT, ignore_errors=True)
raise SystemExit(1 if fail else 0)
