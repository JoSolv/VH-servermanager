# Design note: a built-in Discord bot

Status: **proposal, nothing implemented.** This is the design to argue with
before any code is written.

## What it is for

Three jobs, in decreasing order of how safe they are:

1. **Announce.** The manager already knows when a server starts, crashes, gets
   a new build, loads a mod it has not restarted into, or loses its last
   player. Right now all of that is only visible to whoever has the dashboard
   open. Announcing it to a channel is telling the people it happens to.
2. **Answer.** `status`, `version`, `mods`, `players`, player stats — questions
   the people on the server ask the person who runs it, which the manager can
   answer without that person being awake.
3. **Act.** `start`, `stop`, `restart`, so a server that fell over at 3am does
   not stay down until the operator wakes up.

The third one is a different kind of feature from the first two and the design
treats it that way throughout. See [Permissions](#permissions-the-part-that-matters).

## Shape: one bot, per-instance channels

A Discord bot application is one token and one identity. Running one gateway
connection per instance would mean one bot account per server, N heartbeat
loops, and N bots in the member list, for nothing. So:

- **The token and the connection are manager-level**, like the Thunderstore
  index and the scheduled server update. Configured once in Settings.
- **Announcement routing is per-instance.** Each instance gets its own
  `Discord` panel on its page: a channel id and an on/off switch. An instance
  with no channel set announces nothing, which is what "optional" means at the
  level people actually want it — one server posting to `#midgard`, another to
  `#modded-test`, a third silent.
- **Event toggles are global** in the first version (announce joins: yes/no),
  because which *kinds* of thing you want to hear about is a taste, not a
  per-server fact. Per-instance overrides can come later as a dict whose
  missing keys mean "inherit".

## Transport: the gateway, outbound only

Discord offers two ways to receive a slash command:

- **An HTTP interactions endpoint.** Discord POSTs to a public HTTPS URL that
  you verify with an ed25519 signature.
- **The gateway.** A websocket the bot opens *outbound*, over which Discord
  delivers `INTERACTION_CREATE` like any other event.

**The gateway, and it is not close.** This manager binds to `127.0.0.1` by
default and has no authentication precisely because it can start processes on
the host; the typical deployment is a TrueNAS or Unraid box behind NAT. An
interactions endpoint would require publishing an HTTPS URL from that machine,
which is the one thing the current security posture is built to avoid. The
gateway needs no inbound port, no certificate and no DNS.

Replies go back over plain REST (`POST /interactions/{id}/{token}/callback`),
so the socket is only ever read.

### No new dependencies

- `httpx` is already a direct dependency, and that is the whole REST side.
- `websockets` is already installed, as part of `uvicorn[standard]`. It should
  be pinned explicitly in `requirements.txt` rather than leaned on as a
  transitive — nothing new gets downloaded, the dependency just stops being
  accidental.

That makes `discord.py` a question worth asking rather than an obvious yes. It
would handle gateway resume, rate-limit buckets and command registration for
us. Against it: it is a large surface for the six commands we want, it brings
`aiohttp` alongside the `httpx` already here, and this project hand-rolls the
A2S binary protocol, nftables counters and BepInEx's config format rather than
taking a dependency for each. A minimal gateway client is about 300 lines and
in character. **Recommendation: hand-rolled, with `discord.py` as the fallback
if the reconnect edge cases turn out worse than they read.**

### Slash commands only, so no privileged intents

Prefix commands (`!status`) require the **Message Content** intent, which is
privileged and needs justification to Discord once a bot is in more than a
handful of guilds. Slash commands need **no intents at all** — the identify
can send `intents: 0`. One less thing that can be switched off underneath a
working install.

## Where the announcements come from

The manager already produces everything needed; almost none of this is new
monitoring. The proposal is a small `vhsm/events.py` — an `EventBus` shaped
like the existing `Hub`, with typed events instead of snapshot JSON — fed from
the places listed below. The Discord bot is one subscriber; a webhook sink or
an email sink later would be another.

| Announcement | Where it comes from today |
|---|---|
| starting / running / stopped / crashed | `Supervisor.status` transitions |
| "ready", as opposed to "the process exists" | `record.listening` plus `record.version` |
| crash detail | `supervisor.exit_code`, `supervisor.last_error` |
| recognised failures (missing libraries, no user account) | `supervisor.notices`, already keyed for change detection |
| player joined, by character name | `PlayerTracker` `named` event |
| player left, with time played | `PlayerTracker` `disconnect` event |
| player died | `PlayerTracker` `died` event (the tracker already spots `ZDOID … 0`) |
| mods changed since the server started | `record.restart_pending` |
| a mod has a newer version | `record.mod_notices`, key `mod-updates:<pkg>-<ver>` |
| scheduled mod check result | `ModUpdateRun.summary()`, already one human sentence |
| a server build is published | `manager.check_update()` |
| update installed, instances restarted | `run_update()` |
| reachable / unreachable from outside | `record.reachable` (tri-state) |
| automatic snapshot taken | `_auto_snapshot()` |
| the manager itself started or stopped | app lifespan |

Two things are worth getting right rather than shipping naively:

- **Flapping.** `reachable` is a UDP round trip and will fail transiently.
  Announce only after N consecutive samples agree, not on every change.
- **A burst at boot.** Nothing should re-announce state that merely became
  visible again because the manager restarted. Transitions are only announced
  when both sides were observed *in this run*, with a short quiet period after
  startup — the same "recognised in this run" rule `IssueWatcher` already uses.

### Sources needing a small change

- `PlayerTracker` takes a single `on_event` callback and the manager has
  already spent it on `roster.observe`. It needs to fan out to two listeners.
- `InstanceManager.submit()` should take an optional `actor` (default `"web"`),
  so an announcement can say *restarted by @someone* and the audit trail has a
  name in it. This is the same hook the audit log needs, so it pays twice.

## Command surface

Namespaced under one command so it never collides with another bot:

**Read**

| Command | Answers with |
|---|---|
| `/vh servers` | every instance, one line each: state, players, version |
| `/vh status [server]` | state, uptime, version, players *n*/*max*, join address, mod count, whether a restart is pending |
| `/vh players [server]` | who is on, character name, time played, platform |
| `/vh mods [server]` | active mods and versions, and what has an update |
| `/vh player <name>` | from the roster: sessions, total time played, last seen — works for someone who left an hour ago, which is the whole point of the roster |
| `/vh version` | manager build, running server build, installed vs newest Steam build |

**Control** — off until switched on, and separately gated

| Command | Notes |
|---|---|
| `/vh start [server]` | |
| `/vh restart [server]` | |
| `/vh stop [server]` | asks for confirmation with a button when anyone is online; a stop disconnects people |

`[server]` is optional when only one instance exists, and autocompletes from
instance names otherwise (autocomplete is just another interaction type over
the same socket).

Control commands reply immediately — `submit()` returns as soon as the task is
owned by the manager — and the *completion* arrives through the announcement
path, which is already watching the status transition. No polling in the bot.

### Optional: a sticky status message

One message per instance, pinned, edited in place as state changes: the
dashboard card, in the channel. Nice, and cheap to add once announcements
exist, but it needs throttling (edit no more than every ~15s, and only when
the rendered text actually changed) or it burns the channel's rate limit for
no benefit. Phase 4.

## Permissions: the part that matters

The README says, correctly, that there is no authentication and that the GUI
binds to localhost because it can start processes on the host. **A control
command is a hole through that**, and it must be designed as one and not as a
convenience. Letting "members of the channel" run `stop` means letting anyone
who can get into that channel — including anyone an admin invites next month —
stop the server.

So, in order:

- **Control is off by default.** Reads and announcements work with control
  disabled, and that is the configuration most people should run.
- **Pinned to one guild.** The bot ignores everything outside the configured
  guild id, so a token that leaks or an install into a second server cannot
  reach the host.
- **Channel allowlist.** Commands are only honoured in the channels listed.
  Discord's own channel permissions are the first gate; this is the second,
  because the bot's permissions are managed by Discord admins and this list is
  managed by the operator.
- **Role and user allowlists, separately for read and control.** By role id and
  user id, not "whoever can see the channel". An empty read allowlist means
  anyone in an allowed channel; an empty *control* allowlist means nobody, and
  control stays off.
- **Confirmation on `stop`** when players are online, and the button only
  answers the user who ran the command.
- **A cooldown per user and per instance**, so `restart` cannot be held down.
- **An audit trail.** Every control command recorded with the Discord user id
  and name, into the manager log and into the instance's own log, and shown in
  the UI. If it can take the server down, there should be a record of who did.
- **Ephemeral replies** for anything that is nobody else's business, and the
  server password never appears in any reply or embed. The join address is
  posted only if *Show join address* is on.

### The token

- Stored at `<data_root>/discord_token`, mode `0600`, **not** in `manager.json`
  and not in any instance's `instance.json`.
- Overridable by `VHSM_DISCORD_TOKEN`, so container users can keep it in a
  Docker secret or their compose env and never write it to the data volume.
- **Never rendered back into the form.** Unlike the server passwords, which the
  README admits are plaintext because the server needs them on a command line,
  a bot token is an identity. Settings shows *configured* / *not configured*
  with **Replace** and **Clear**, and that is all.
- Excluded from the `.vhsm.zip` instance archive by construction, since it is
  manager-level and the archive is per-instance.

One related trap: `discord_channel_id` *does* live in `instance.json`, so an
exported instance carries it to another host, where it would start announcing
into a channel that host's operator cannot see. It should be cleared on import,
the way identity and port already are. Clone keeps it — same host, same people.

### Inviting the bot

Settings should build the OAuth2 install URL once connected, from the
application id in the gateway's READY payload: scopes `bot` and
`applications.commands`, permissions limited to **View Channel**, **Send
Messages**, **Embed Links** (plus **Read Message History** if the sticky status
message is enabled). "Here is the link, click it, pick the channel" is a much
better answer than a paragraph telling someone to go build an application.

Commands register guild-scoped (`PUT /applications/{app}/guilds/{guild}/commands`),
which takes effect immediately; global registration can take an hour and is
wrong for a bot that lives in exactly one guild.

## Module layout

```
vhsm/events.py            EventBus + Event: the one place announcements come from
vhsm/discord/
  config.py               DiscordConfig, load/save, token file + env override
  gateway.py              websocket: identify, heartbeat, resume, reconnect/backoff
  rest.py                 httpx: send/edit, register commands, interaction callback, 429
  commands.py             command definitions, permission gate, handlers
  announce.py             event -> embed, channel routing, throttling, dedupe
  bot.py                  DiscordBot: owns the task, subscribes to the bus
tools/fake_discord.py     offline stand-in, like tools/fake_valheim_server.py
```

Touched: `config.py` (paths), `instance.py` (two fields, and `from_dict`
already ignores unknown keys so old instances load unchanged), `manager.py`
(the bus, the `PlayerTracker` fan-out, the `actor` argument), `web/routes.py`
and `settings.html` (a panel), `instance.html` (a panel), `archive.py` (clear
the channel on import), `tests/smoke_test.py`, `README.md`.

The dependency runs one way: `vhsm/discord/*` imports from the manager, and the
manager knows only about `EventBus`. A bot that fails to connect must never
stop the manager from booting — a bad token is a notice in Settings, not a
crash, and `4004 Authentication failed` stops retrying rather than hammering
Discord forever.

## Testing it offline

The smoke suite needs no network and no Steam download, and this must not
change that. The gateway and API base URLs become settings, and
`tools/fake_discord.py` serves a stub gateway and REST surface locally, exactly
as `tools/fake_valheim_server.py` stands in for the real binary. Then the
checks are ordinary:

- identify, heartbeat, resume after a dropped socket, and stop on `4004`
- commands registered once on READY
- `/vh status` returns the state the manager is actually in
- a control command from a user not on the allowlist is refused, and refused
  the same way when the allowlist is empty
- an allowlisted user's `/vh restart` reaches `manager.submit` with the actor
  recorded
- a status transition produces exactly one message, to the right channel
- a manager restart does not re-announce what it merely re-observed

## What it will not do, and why

Worth writing down now, because these are the first three things anyone will
ask for:

- **No chat relay.** Valheim's chat is an RPC between clients that the
  dedicated server only forwards; it never reaches the console, which is the
  manager's only window into the game. Reading or injecting chat needs a
  server-side mod, and that is a different feature from this one.
- **No in-game commands** (`/vh kick` reaching the game directly). A stock
  server has no RCON and ignores stdin — which is why the existing kick is a
  brief self-lifting ban written to a file. Moderation commands *could* go
  through that same path later, and that is a reasonable phase 5, but it is
  worth being honest that "kick" means "ban for a minute" underneath.
- **No ping.** Already a known gap: Valheim does not expose per-player latency
  anywhere, so the bot cannot report what the dashboard will not invent.

## Phases

1. **Transport.** Config, token storage, gateway + REST, connection status and
   the invite link in Settings, and `/vh servers` / `/vh status` / `/vh players`.
   Read-only, no announcements. Proves the socket and the test stub.
2. **Announcements.** The event bus, per-instance channel, global toggles,
   status and player and mod and update events, with the flapping and
   boot-burst rules above.
3. **Control.** `start` / `stop` / `restart` behind the full gate: guild,
   channel, role, cooldown, confirmation, audit trail.
4. **Extras.** `/vh mods`, `/vh player`, autocomplete, death announcements, the
   sticky status message, per-instance event overrides.

Each phase is useful shipped alone, and phase 1 is where the uncertainty is —
if the hand-rolled gateway is going to be a mistake, it will be obvious there,
before anything depends on it.
