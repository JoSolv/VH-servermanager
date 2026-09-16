# Valheim Server Manager (vhsm)

A web GUI for running several Valheim dedicated servers on one host: create and
configure instances, start and stop them, watch live resource usage and player
counts, and manage mods per instance the way r2modman does on the desktop.

This is a first working version — the core of each area is implemented and
tested end to end, and the structure is meant to be built on.

![dashboard](docs/dashboard.png)

## What works today

**Instances**
- Create, edit and delete instances from the GUI; each lives in its own
  directory and is described by an `instance.json`, so it can be copied,
  backed up or restored by moving a folder.
- Full launch configuration: name, world, password, port, public/crossplay,
  world preset and the five world modifiers, autosave and backup intervals,
  and free-form extra arguments.
- Validation mirrors what the server itself enforces (password length, the
  password not appearing in the server or world name, public servers needing a
  password) plus port-range collision checks between instances.

**Lifecycle**
- Start/stop/restart. Each server runs in its own process session, and stopping
  sends `SIGINT` first — Valheim's graceful shutdown, which saves the world —
  escalating to `SIGTERM` then `SIGKILL` only if it does not comply.
- Lifecycle actions run as manager-owned background tasks, so a browser that
  navigates away (or a button that re-renders mid-request) cannot abandon a
  restart half-finished. The UI shows `starting` / `stopping` / `restarting`
  while the work is in flight.
- Console output is streamed to the browser over a websocket and mirrored to
  `logs/console.log`.
- `autostart` boots flagged instances with the manager.

**Monitoring**
- Per-instance CPU, RSS, memory share, thread count and disk I/O via `psutil`,
  rolled up over the process tree. CPU is shown as a share of the whole host
  with the core count alongside: psutil's raw figure is summed across cores, so
  a server using three cores reports 300%, which reads as a broken gauge.
- The running server's version, taken from its console banner and confirmed
  against the Steam query response, on every card.
- Live player count from the Steam A2S query socket (`game port + 1`), with
  player *names* and platform ids parsed from the console log, since A2S does
  not report them reliably for Valheim. Each connected player shows their id,
  platform, playtime and admin/ban/permit flags.
- Per-instance network throughput via nftables counters on each instance's UDP
  port range. Linux has no per-process byte counters, so this is the honest way
  to get it; it needs root. Without it the UI says so and still shows host-wide
  traffic rather than pretending.
- Host CPU, memory, load average and network in the dashboard header.
- A **client visibility** probe on each instance that queries the server the way
  a Valheim client does, so "shows as unreachable in the client" can be told
  apart from "the game port is fine".

**Moderation**
- Admin, banned and permitted lists edited from the GUI. Valheim re-reads these
  files while running, so changes take effect within seconds without a restart.
- Quick actions on every connected player: make admin, ban, and kick. A stock
  server exposes no RCON and ignores stdin, so kick is a brief ban that lifts
  itself; the pending expiry is written to disk, so a manager restart mid-kick
  still clears it rather than stranding the player on the banned list.

**Updates**
- The installed build id (from steamcmd's app manifest) is compared against the
  newest published build, so the dashboard can say whether an update exists
  rather than guessing from file dates.
- One-click update that stops running instances, updates, and starts them again.
- Optional daily scheduled update at a chosen time, with the same stop/start
  handling.

**Mods (r2modman-style)**
- Browse and search the full Thunderstore catalogue for Valheim, cached to disk
  so searching is instant and works offline.
- Install with recursive dependency resolution; BepInEx is pulled in
  automatically because a Valheim mod cannot load without it.
- A shared package cache (`cache/<namespace-name>/<version>/`) means a package
  is downloaded once and installed into any number of instances from disk.
- r2modman's install rules: `plugins/`, `patchers/`, `monomod/`, `core/` and
  `config/` are routed to the right place, and anything else lands in
  `BepInEx/plugins/<Author>-<Mod>/` so uninstalling is exact.
- Enable/disable without deleting (files are renamed `.old`, as r2modman does).
- Config files are never renamed on disable or deleted on uninstall, and never
  overwritten on update — tuned settings survive.
- Update detection with one-click update-all, orphaned-dependency pruning,
  profile export/import, and manual `.zip` upload for packages not on
  Thunderstore.

## Requirements

- Linux, Python 3.11+
- `nftables` and root **only** for per-instance network graphs; everything else
  works unprivileged
- ~2 GB disk for the shared dedicated-server install

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Development: simulated servers, no Steam download needed
VHSM_FAKE_SERVER=1 .venv/bin/python run.py

# Production
.venv/bin/python run.py
```

Then open <http://127.0.0.1:8080>, go to **Settings** and install the dedicated
server (this runs `steamcmd +app_update 896660`), then create an instance.

`VHSM_FAKE_SERVER=1` swaps the real binary for `tools/fake_valheim_server.py`,
which emits Valheim-shaped console output, answers A2S queries and simulates
players joining and leaving. It makes the whole GUI exercisable — and the test
suite runnable — without downloading the game.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `VHSM_DATA_ROOT` | `./data` | Everything the manager owns |
| `VHSM_HOST` | `127.0.0.1` | Bind address |
| `VHSM_PORT` | `8080` | Bind port |
| `VHSM_SAMPLE_INTERVAL` | `2.0` | Seconds between metric samples |
| `VHSM_INDEX_TTL` | `3600` | Thunderstore catalogue cache lifetime |
| `VHSM_FAKE_SERVER` | unset | Use the simulated server |

CLI flags `--host`, `--port`, `--data-root`, `--fake-server` and `--reload`
override these.

### Data layout

```
data/
  steam/                  steamcmd
  valheim/                shared dedicated-server install (app 896660)
  cache/                  Thunderstore packages + catalogue, shared
  instances/<id>/
    instance.json         configuration
    mods.json             installed mods, versions, owned files
    saves/                worlds and backups
    logs/console.log
    BepInEx/              this instance's mod profile
```

One shared game install keeps updates to a single download no matter how many
servers you run; instances differ only by their own directory. Mods are loaded
by pointing Unity Doorstop at the instance's own `BepInEx` tree with absolute
paths, so a modded and an unmodded instance differ only by what is on disk.

## Tests

```bash
.venv/bin/python tests/smoke_test.py
```

44 checks covering page rendering, instance creation and validation, the
start/stop lifecycle, the live-metrics and console websockets, and the whole
mod flow (search, dependency resolution, disable/enable, dependency-protected
uninstall, export). Runs against the simulated server, so it needs no network
and no Steam download.

## Security

**There is no authentication yet.** The GUI can start processes and write files
on the host, so it binds to `127.0.0.1` by default. If you expose it, put it
behind a reverse proxy that authenticates, or reach it over an SSH tunnel.
Binding to anything else logs a warning and shows one in Settings. Server
passwords are stored in plain text in `instance.json` and rendered into the
edit form, as the server needs them on its command line.

## Known gaps / next steps

- No authentication or multi-user roles.
- **Ping is not shown, because Valheim does not expose it.** There is no RCON,
  the console never logs latency, and `A2S_PLAYERS` carries only a connection
  duration (which is shown). A per-player ping would have to be invented, so
  the field is reported as `null` rather than filled with a guess.
- No RCON-style console input — commands would need a server-side mod.
- No scheduled *restarts* (scheduled updates exist), backup browser/restore, or
  crash auto-restart.
- Mod install runs inline in the request; large packages block that request.
  Moving it to a background job with progress in the UI is the natural next step.
- `.r2x` files exported by r2modman itself are not yet parsed (our own JSON
  export/import is); the importer already understands its `{major, minor, patch}`
  version shape.
- Per-instance network needs root. A rootless fallback would need eBPF.

### If a server shows as unreachable in the client

The entry's name, player count and version all come from the **Steam query
port** (`game port + 1`), not the game port — so a server can accept direct
joins while its listing looks broken. Use **Test client visibility** on the
instance page to see which of the two is failing. Things worth checking:

- UDP `port`, `port+1` and `port+2` all need forwarding, not just the game port.
- The server is launched from the game directory, as upstream's
  `start_server.sh` does, and `steam_appid.txt` is written next to the binary.
  Launching from elsewhere can leave Steam's game-server API half-initialised,
  which produces exactly this symptom — direct joins work, the query port never
  answers. If you ran an earlier build of this manager, this is worth re-testing.
- With crossplay enabled the server is meant to be joined by its join code
  rather than through the Steam list, so the Steam entry can look wrong
  regardless of configuration.
