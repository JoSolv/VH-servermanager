# Running vhsm on TrueNAS

A walkthrough for running the manager as a TrueNAS app.

**You need TrueNAS SCALE 24.10 "Electric Eel" or later.** That release replaced
the old Kubernetes-based apps with Docker, which is what gives you the *Install
via YAML* option used below. On an older release (Bluefin, Cobia, Dragonfish)
there is no way to paste a compose file, and you would have to run the manager
outside the apps system instead.

No Docker experience assumed — the short version of what is happening:

- **An image** is a prepared, frozen copy of the app and everything it needs to
  run. GitHub builds one for you and stores it at `ghcr.io`.
- **A container** is one running copy of that image. TrueNAS starts it for you.
- **A volume** is a folder on your pool that the container can write to. Without
  one, everything the container writes disappears when it restarts — including
  the ~2 GB of Valheim server files and, more importantly, your worlds.

You will do four things: publish the image, make it public, create a dataset,
and paste a short YAML file into TrueNAS.

---

## 1. Build and publish the image

The repository already contains the recipe (`Dockerfile`) and the automation
(`.github/workflows/publish-image.yml`). GitHub does the building.

1. Open the repository on GitHub → **Actions** tab.
2. If it asks you to enable workflows, do that.
3. Pick **Publish container image** in the left-hand list.
4. **Run workflow** → choose your branch → **Run workflow**.

It takes a few minutes. When it finishes you have an image at:

```
ghcr.io/josolv/vh-servermanager:latest
```

> The workflow builds automatically on every push to `main`. Running it by hand
> is only needed while your work sits on another branch.

## 2. Make the image public

**This is the step everyone misses.** A newly published GitHub package is
private, and TrueNAS will fail to pull it with a "denied" or "not found" error
that does not explain why.

1. On the repository's front page, find **Packages** in the right-hand column.
2. Click `vh-servermanager`.
3. **Package settings** → scroll to **Danger Zone** → **Change visibility** →
   **Public**.

(If you would rather keep it private, TrueNAS can store registry credentials
under **Apps → Settings → Manage Container Images**, using a GitHub personal
access token with the `read:packages` scope. Public is simpler.)

## 3. Create a dataset for the data

**Datasets → Add Dataset** on your pool. Name it something like `vhsm`. The
path will look like `/mnt/tank/apps/vhsm` — note it down, substituting your own
pool name for `tank`.

This one folder holds the Valheim server files, every world, all backups, mods
and settings. Snapshot it with TrueNAS's own snapshots if you want belt and
braces on top of the manager's world snapshots.

## 4. Install the app

**Apps → Discover Apps → the three dots at the top right → Install via YAML.**

Give it a name (`vhsm`), then paste the contents of
[`docker-compose.yaml`](../docker-compose.yaml) from the repository, changing:

- the volume path to your dataset from step 3
- `TZ` to your timezone

Then install. The first start is quick — the image is small, because the
Valheim server files are *not* baked into it. You download those next, from
inside the app.

## 5. First run

Open `http://<your-nas-ip>:8080`.

1. **Settings → Install server** — this runs steamcmd and fetches the dedicated
   server files (~2 GB) into your dataset. Watch the log on that page.
2. **Settings → Server address** — set the hostname or IP your players will
   use. This is what the manager shows beside each instance and what its
   reachability check probes.
3. **New instance** — name it, set a world name and a password of at least five
   characters, and start it.

---

## Ports

Each instance uses **three consecutive UDP ports** starting at its game port:
2456, 2457 and 2458 for the first one. A second instance on 2466 uses
2466–2468, and so on.

With the host networking in the supplied YAML, those ports are open on the NAS
as soon as an instance starts — nothing to redeploy. To let people outside your
network in, forward the same UDP ranges on your router to the NAS.

If you run the server with **crossplay** enabled, port forwarding is not needed:
crossplay routes players through Microsoft's PlayFab relay and they join with a
code rather than an address.

## Security

**The manager has no login.** Anyone who can reach port 8080 can start
processes on your NAS. On a home LAN that is usually acceptable; do not forward
port 8080 on your router. If you want it reachable from outside, put it behind
something that authenticates — a reverse proxy with basic auth, Tailscale, or a
VPN.

## Updating the app

Two separate things have to happen: a new image has to be **built**, and your
NAS has to **pull** it.

**1. Build it.** Pushing to any branch builds automatically — watch the
**Actions** tab until the run goes green. To build without pushing, use
**Actions → Publish container image → Run workflow**.

Only the repository's *default* branch updates the `:latest` tag. Other
branches publish under their own name (`claude-my-branch`), so if you are
working on a side branch, check which tag the green run produced — the compose
file pulls `:latest`.

**2. Pull it.** Either:

- **Apps → vhsm → Update**, if TrueNAS is offering it. TrueNAS does watch
  upstream images for custom apps, so this generally works.
- **Apps → vhsm → the three dots → Edit → Save**, which redeploys. The supplied
  compose file sets `pull_policy: always`, which matters: without it Docker
  reuses a tag it already has on disk, so redeploying an app pinned to
  `:latest` fetches nothing and the update silently does not happen.

### Do not trust the "Update available" column with `latest`

A moving tag like `latest` does not give TrueNAS a version to compare, only a
digest, and the indicator is known to get stuck showing "Update available"
even after a successful update. Treat it as a hint, never as confirmation.

**Check the build instead.** The image stamps in its commit, and the manager
reports it in two places:

- the container log, on the first line at startup:
  `vhsm 0.1.0 (container image, commit e045c7f, built 2026-...)`
- **Settings → This manager**

Compare that commit against the one the green Actions run built. If they match,
the update landed, whatever the column says. If it says *running from a source
checkout*, you are not running a built image at all.

Your dataset is untouched by any of this, so worlds, mods and settings survive.

## If something does not work

**The app will not pull the image.** Almost always step 2 — the package is
still private. The error mentions `denied` or `manifest unknown`.

**The app starts but the page does not load.** Check something else is not
already on port 8080 (TrueNAS itself uses 80 and 443). Change `VHSM_PORT` in
the YAML and redeploy.

**"Server binary missing" when starting an instance.** The server files have
not been installed yet — do step 5.1.

**steamcmd downloads fine, then fails with `Missing file permissions`
(exit code 8).** Look for a line like `Redirecting stderr to '//Steam/logs'`
earlier in the log. steamcmd keeps its own state under `$HOME/Steam`, and
`//Steam` means `HOME` was the container root, which your app user cannot
write. Images built before this was fixed inherit that `HOME`; pull the latest
image and it will use `/data/home` instead. The manager now also checks those
directories before running steamcmd and names the one it cannot write.

**The app exits immediately with "Cannot create /data/…: Permission denied".**
The mounted dataset is not writable by the user the container runs as. Either
set `PUID`/`PGID` to an account that owns it, or fix the ownership on the
dataset (in TrueNAS: **Datasets → your dataset → Permissions → Edit**).

**The network panel says per-instance accounting is unavailable.** The
container does not have `NET_ADMIN`, or is running as a non-root user via
PUID/PGID. Everything else works; only the per-instance network graph is
affected.

**An instance says "up, query silent" or the visibility check finds nothing.**
The server is running and holding its game port — that part is confirmed from
the kernel, not guessed. What did not answer is the Steam query socket, which
is what fills in a server's entry in the browser. With crossplay on, that is
expected and harmless. With crossplay off, check that UDP game-port+1 is
forwarded, and use **Test client visibility** on the instance page: it lists
every UDP socket the server process actually holds.
