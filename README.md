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
  platform and playtime; admin/ban/permit state lives on the Players roster.
- Per-instance network throughput via nftables counters on each instance's UDP
  port range. Linux has no per-process byte counters, so this is the honest way
  to get it; it needs root. Without it the UI says so and still shows host-wide
  traffic rather than pretending.
- Host CPU, memory, load average and network in the dashboard header.
- A **client visibility** probe on each instance that queries the server the way
  a Valheim client does and reports the UDP sockets the process actually holds,
  so "shows as unreachable in the client" can be told apart from "the game port
  is fine".

**Players and moderation**
- A roster of everyone who has ever joined, kept across restarts. Valheim's
  console is the only source of player identity a stock server offers and it
  says nothing about anyone not currently connected, so moderating someone who
  left an hour ago means having written down that they were here.
- Per player: names used, platform id, session count, time played, last seen
  ("now" while online, otherwise a timestamp) and a bounded log of their
  connects, spawns, deaths and disconnects — downloadable as a text file.
- Admin, ban, permit and kick from the roster, whether or not the player is
  online. Valheim re-reads its list files while running, so changes take effect
  within seconds without a restart.
- Kick is a brief ban that lifts itself, because a stock server exposes no RCON
  and ignores stdin. The pending expiry is written to disk, so a manager restart
  mid-kick still clears it rather than stranding the player on the banned list.
- **Use Whitelist** is off until you turn it on. Valheim treats a non-empty
  `permittedlist.txt` as a whitelist, so switching it off parks the file rather
  than deleting it — the list survives to be switched back on. Permitting a
  player while it is off writes to the parked copy, so adding somebody never
  locks the server down behind your back.

**Worlds, backups and portability**
- Both save formats are handled. Since Valheim 1.0 a world is a **folder**
  named after it, holding `_main.<N>.db2`, `_main.<N>.fwl2`, `_main.<N>.chunks`,
  an `_main.<N>.ok` marker and many `.chunk` files, with several save
  generations side by side; older worlds are still a `.db` + `.fwl` pair.
  Worlds are always copied whole — a partial copy of a 1.0 world is not a
  smaller world, it is a broken one.
- **A world and a server are separate things**, and so are the two ways of
  moving them. A *world* is the Valheim save — terrain, structures, the map —
  and is moved from the instance that owns it, under *Transfer world*. A
  *server instance* is that world plus everything wrapped around it, and is
  moved from the dashboard, under *Transfer servers*. Moving a world never
  touches configuration, players or mods.
- Import a world under *Transfer world*, from one field that takes either shape
  it arrives in: a zipped world folder, or the folder itself dropped in as-is.
  On an instance with no world yet the instance is repointed at the upload,
  because the server only loads the world its configuration names — without
  that it would ignore the import and generate an empty world instead.
- Importing into an instance that **already has a world** replaces it, so it
  asks first and snapshots the current world before overwriting it. The upload
  is installed under the instance's existing world name, which is what makes it
  a replacement rather than a second world sitting beside the first: the server
  keeps loading the same world, and the snapshot just taken is one click away
  under *Backups*.
- Export a world as a zip of its world folder — the save on its own, ready to
  import into another instance or to play in single-player.
- Snapshots taken on demand or on a per-instance schedule, kept in `backups/`
  inside the instance rather than in `worlds_local`, where a backup folder would
  show up as another world. Automatic snapshots are pruned to a configured
  count; ones taken by hand are never pruned. Valheim's own rotating backups are
  listed alongside them.
- Rollback to any restore point. The live world is snapshotted first, so a
  rollback can itself be undone, and it is refused while the server is running:
  a running server holds the world in memory and would overwrite the restored
  copy at its next autosave.
- Export a whole server instance as one `.vhsm.zip` and import it here or on
  another host, from the single *Transfer servers* section on the dashboard or
  from a card's **⋮** menu. The archive is everything the instance uses —
  configuration, world, access lists, the player roster, mods and their config,
  and the snapshots — so an import is a *clone* of the server it came from
  rather than a reconstruction of it. Only the console transcript is left out:
  it records the original's runs, not anything the copy will use.
- **Clone** a server from the same menu, without a round trip through a file.
  The copy is named `<original> (clone)`, keeps the original's port if it is
  free and otherwise takes the next free 3-port range above it, and is left
  stopped — two servers loading one world would be two servers fighting over
  the same save.
- An import gets a fresh identity, its name gains a number if that name is
  taken, and its port moves aside the same way a clone's does, so a server can
  be imported alongside itself.

**Updates**
- The installed build id (from steamcmd's app manifest) is compared against the
  newest published build, so Settings can say whether an update exists rather
  than guessing from file dates, with an update button beside it. The dashboard
  stays about the host, and says one line when an update is waiting.
- One-click update that stops running instances, updates, and starts them again
  — including when the update fails, since that is a bad reason to leave servers
  down.
- Optional daily scheduled update at a chosen time, with the same stop/start
  handling.
- The full steamcmd transcript is written to `steamcmd.log` and downloadable,
  so a failed install can still be read after its output has scrolled away. Each
  instance's own console log is downloadable from its page for the same reason:
  the box on screen shows a tail, and the file is the whole thing.

**Addressing**
- A global server address (hostname or IP) that players connect to, shown as
  `host:port` beside every instance with a copy button.
- Each instance is probed at that address on a timer and reports whether it
  answered. The probe leaves the host, so it tests the path players take —
  though a router that will not loop traffic back to itself can report a working
  server as unreachable, and the UI says so.

**Interface**
- The instance page's sections (backups, transfer world, players,
  configuration, danger zone) collapse, start collapsed, and remember what you
  opened.
- Each dashboard card carries a **⋮** menu in its top corner for the actions
  that are about the instance rather than its process — manage mods, export it,
  clone it — leaving Start/Stop/Restart as the only buttons that move a server.
- The Players roster has a search box and All/Online/Admins/Banned filters, and
  scrolls in a fixed-height box. Each row carries the one action it needs now --
  Kick while online, Unban while banned -- with the rest behind a per-row
  toggle.

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

## Running it

### As a container (TrueNAS, Unraid, plain Docker)

A prebuilt image is published to GitHub's registry by
[`.github/workflows/publish-image.yml`](.github/workflows/publish-image.yml),
and [`docker-compose.yaml`](docker-compose.yaml) is ready to paste into a
TrueNAS custom app. **[docs/truenas.md](docs/truenas.md) is a step-by-step
walkthrough** that assumes no Docker experience.

```bash
docker run -d --name vhsm \
  --network host --cap-add NET_ADMIN \
  -v /srv/vhsm:/data -e TZ=Europe/Oslo \
  ghcr.io/josolv/vh-servermanager:latest
```

Host networking is the default because a Valheim instance needs three UDP
ports and instances are created from the web UI after the container is already
running — with bridge networking every new instance would mean editing the
port list and redeploying. The image is amd64 only, since the Valheim
dedicated server is an x86_64 binary. Mount `/data`: the server files, worlds,
backups and mods all live there.

### From source

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
| `PUID` / `PGID` | `0` | Container only: own `/data` as this user |
| `TZ` | UTC | Container only: local time for schedules and timestamps |

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
.venv/bin/pip install -r requirements-dev.txt   # pyflakes, for the static gate
.venv/bin/python tests/smoke_test.py
```

270 checks covering page rendering, instance creation and validation, the
start/stop/restart lifecycle, the live-metrics and console websockets, CPU
normalisation, version reporting, player detail, every moderation path, query
socket discovery (including a socket bound to a single interface), the
connectivity probe, instance export/import (including that the world, access
lists, player roster and snapshots all come across, and rejection of a
traversing archive), cloning (naming, port allocation and what is and is not
copied), world export and world import in both save formats (zipped folder,
loose files, and rejection of a traversing filename) in both the fresh-instance
and the replace-an-existing-world cases, snapshots and rollback of a 1.0 folder
world and its undo, automatic snapshots and their pruning, the update endpoints
(including that a failed update still restarts the servers), the player roster
and its history export, the whitelist defaulting to off, the console log
download, the server address and reachability probe, and the whole mod flow
(search, dependency resolution, disable/enable, config preservation,
dependency-protected uninstall, export). Runs against the simulated server, so
it needs no network and no Steam download.

The suite opens with a pyflakes pass over the whole tree, because compiling a
module only proves it parses: a name referenced inside a rarely-taken branch
stays invisible until that branch runs. The suite also creates the shared game
directory up front, so the code paths that only execute when a real install is
present are covered rather than skipped.

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
- No scheduled *restarts* (scheduled updates exist) and no crash auto-restart.
- Backups are Valheim's own; the manager does not run its own backup schedule
  beyond the snapshots it takes before a rollback and before an import
  overwrites a world.
- Exporting or cloning a *running* server copies its world while the server
  still holds it in memory, so the copy can be a moment behind or mid-write.
  Stop it first if the copy has to be exact.
- Mod install runs inline in the request; large packages block that request.
  Moving it to a background job with progress in the UI is the natural next step.
- `.r2x` files exported by r2modman itself are not yet parsed (our own JSON
  export/import is); the importer already understands its `{major, minor, patch}`
  version shape.
- Per-instance network needs root. A rootless fallback would need eBPF.

### Bringing an existing world in

From another dedicated server, or from single-player:

1. Find the world. Since Valheim 1.0 it is a **folder** named after the world
   inside `worlds_local` — on Linux that is
   `~/.config/unity3d/IronGate/Valheim/worlds_local/<World>/` when the old
   server ran without `-savedir`, otherwise under the `-savedir` it was given.
2. Zip that folder (the folder itself, not just its contents).
3. On the instance page, stop the server, then open **Transfer world** and
   either browse for the zip or drop the world folder straight onto the import
   field.

On an instance that has not generated a world yet, it is repointed at whatever
you upload, so the old trap of the configured world name not matching the
folder on disk — which makes Valheim silently generate a fresh empty world —
does not apply. On one that already has a world you are asked to confirm, the
current world is snapshotted, and the upload takes its place under the same
world name. Upload the whole folder either way: one complete `_main.<N>`
generation and its `.ok` marker must be present, and the panel says so if they
are not.

Copying files in by hand still works. Put the world folder in
`<data_root>/instances/<id>/saves/worlds_local/` and set the instance's world
name to match the folder exactly — it is case-sensitive. Access lists go in
`<data_root>/instances/<id>/saves/` as `adminlist.txt`, `bannedlist.txt` and
`permittedlist.txt`.

### If a server does not appear in the server browser

Check **List in the server browser** on the instance first. Without it the
server launches with `-public 0` and is never advertised: it runs, it accepts
players who type its address, and it is absent from the list — which is
indistinguishable from a network problem unless you know to look. New
instances default to listed; older ones keep what they were saved with, and
the connectivity probe calls it out.

The manager also checks that the server's shared libraries resolve, because
Steam's `steamclient.so` is loaded at run time and a missing dependency there
fails silently in the same shape: the game runs while Steam never initialises,
so the query port stays quiet and the server never registers. **Settings**
reports anything unresolved, with the Debian package that provides it.

### If a server shows as unreachable in the client

A server's listing — its name, player count and version — comes from the
**Steam query socket**, not the game port, so a server can accept direct joins
while its entry looks dead.

Earlier versions of this manager probed `127.0.0.1` at `game port + 1` and
called the port closed when nothing answered. Both halves of that were
assumptions, and either being wrong looks identical from outside:

- the query socket is not guaranteed to sit at `game port + 1`;
- a socket bound to **one interface** is unreachable over loopback, so a
  perfectly healthy server reports "port not open".

The manager owns the server process, so it now asks the kernel which UDP
sockets that pid holds and probes the address each one is actually bound to,
instead of guessing. The same discovery feeds the live player count, so a
server that binds to a single interface is no longer invisible to it.

**Test client visibility** on the instance page shows the sockets the process
holds, every address tried, and which one answered. From there:

- Only the game port open, nothing else → the server never created a query
  socket. That points at Steam's game-server API failing to initialise rather
  than at a firewall.
- A socket answered on a port that is not `game port + 1` → forward that port.
- Everything answers locally but the client still cannot see it → UDP `port`
  through `port+2` need forwarding; the listing uses the query port.
- Crossplay servers are joined by their join code rather than through the Steam
  list, so the Steam entry can look wrong regardless of configuration.
