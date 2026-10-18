# Design note: a built-in Discord bot

Status: **announcements and read-only commands implemented, every setting per
instance.** One bot account; each instance names its own Discord server,
channel, announcements and permissions, and a Discord only ever sees its own
servers. Control commands are deliberately not here: the permission tier they
need is built, enforced and tested, but no command uses it yet. See
[Phases](#phases) at the end for what landed and what did not.

This note is the design, kept as the record of *why* it is shaped this way.
Where the implementation settled somewhere different from the proposal, the
text says so rather than being quietly corrected.

**To actually set the bot up**, see [`discord-setup.md`](discord-setup.md).
This file is about the reasoning, not the clicking.

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

## Shape: one bot account, everything else per instance

A Discord bot application is one token and one identity, but that identity can
be invited to any number of guilds. So "a separate Discord per server" needs
separate *guild ids*, not separate bots — and one gateway connection still
serves all of them. Running a connection per instance would mean one bot
account per server, N heartbeat loops and N bots in every member list, for
nothing.

- **The token and the connection are manager-level**, because there is one bot
  account. That and an on/off switch is the whole of Settings.
- **Everything else belongs to an instance**: which guild, which channel, which
  events, whether commands answer about it, who may ask, whether its join
  address may be posted, and (later) who may control it. Stored in that
  instance's `instance.json`, so it travels with the instance.

The first version had the guild and the event switches manager-wide, with only
the channel per instance. That was wrong, and the reason is worth recording:
**the guild is the scope of everything else.** Once two instances can live in
two unrelated Discords, "which guild" stops being a deployment detail and
becomes the thing that decides who can see what. A single global guild also
quietly assumed every server shares one audience, which is exactly what the
people running a public server and a modded test server do not have.

The event switches moved for a plainer reason: a busy public server wants to
say nothing about joins while a server of four friends wants to say everything,
and that is a per-server fact after all, not a taste.

### What the guild scopes

An interaction arrives from one guild. The instances in scope are the ones
whose own settings name that guild *and* whose channel and role allowlists let
this caller ask. Everything works from that set:

- `/vh servers` lists it, and nothing else
- `/vh status` with no argument defaults to it when it holds one instance
- autocomplete offers only its names
- `/vh player` searches only its rosters
- an instance outside it is unreachable **by name or by id**, and the refusal
  is the same one a typo gets

A guild with nothing mapped to it is told only "no servers here are set up to
answer you" — which covers "none configured", "commands off", "wrong channel"
and "wrong role" with one sentence that reveals none of them. This is the
property that makes the feature safe to hand to somebody else's Discord: a
leaked invite exposes at most what deliberately lives there.

Command registration follows the same scope. Each guild gets its own `PUT
/applications/{app}/guilds/{guild}/commands` built from the instances mapped to
it, so two Discords get two menus; and a guild that stops having any instance
has its commands removed rather than left answering "no servers here" forever.

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
monitoring. A small `vhsm/events.py` — an `EventBus` shaped like the existing
`Hub`, with typed events instead of snapshot JSON — is fed from the places
listed below. The Discord bot is one subscriber; a webhook sink or an email
sink later would be another.

Routing happens at the far end, in the announcer, and every destination belongs
to an instance. An event about one instance goes to that instance's channel if
that instance announces that kind of thing. An event about *no* instance — a new
Valheim build, the manager restarting — has no single home now that there is no
manager-wide channel, so it fans out to each distinct channel among the
instances that want it: two servers sharing a channel get one message, two
servers in two Discords get one each. One channel refusing a message does not
stop the others, since on a manager-wide event they belong to different
Discords and one lost permission is no reason for the rest to hear nothing.

| Announcement | Where it comes from today |
|---|---|
| starting / running / stopped / crashed | `Supervisor.status` transitions |
| "ready", as opposed to "the process exists" | `record.listening` **or** `record.query_answered` |
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

That "or" is a correction to the original proposal, which said `listening`
alone. `listening` is the stronger fact -- the kernel saying the process holds
its game port -- but it is only recomputed on the external reachability pass,
every fifteen sampling ticks, so a "your server is up" gated on it alone lands
up to half a minute late. Whether the Steam query socket has answered says the
same thing for a player's purposes and is refreshed every three ticks, so
either will do and the sooner one usually wins. `query_answered` was added to
the instance snapshot for this, and is a useful fact in its own right.

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

`[server]` is optional when only one instance is visible in that guild, and
autocompletes from the visible ones otherwise (autocomplete is just another
interaction type over the same socket).

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

So, in order. Each item is marked with where it stands, since the control
commands themselves are not shipped:

- **Control is off by default, per instance.** *Built.* Reads and announcements
  work with control disabled, and that is the configuration most people should
  run -- and today the only one, since no control command exists. Being per
  instance matters: a test server can be restartable by its three users while
  the public one is restartable by nobody, which is not expressible with a
  single switch.
- **Each instance pinned to its own guild.** *Built.* An interaction can only
  reach instances whose own settings name the guild it came from, so a token
  that leaks or an invite into a second Discord reaches at most the servers
  that deliberately live there — not the host. An instance elsewhere is not
  merely unlisted: it cannot be named or addressed by id either, and the
  refusal is the one a typo gets.
- **Channel allowlist, per instance.** *Built.* Commands about an instance are
  only honoured in the channels it lists. Discord's own channel permissions are
  the first gate; this is the second, because the bot's permissions are managed
  by Discord admins and this list is managed by the operator.
- **Role and user allowlists, per instance, separately for read and control.**
  *Built.* By role id and user id, not "whoever can see the channel". An empty
  read allowlist means anyone in an allowed channel; an empty *control*
  allowlist means nobody, which is the asymmetry that matters and is tested as
  such. Two instances in one guild can have different answers.
- **A cooldown per user and per instance**, so `restart` cannot be held down.
  *Built* (`Cooldowns`), unused until there is a control command to limit.
- **An audit trail.** *Half built.* `submit()` takes an `actor`, so every
  lifecycle action names who asked in the manager log and in a `LIFECYCLE`
  event. Nothing renders it in the GUI yet, which it should before a control
  command ships: if it can take the server down, there should be a record of
  who did that somewhere better than `grep`.
- **Confirmation on `stop`** when players are online, with the button only
  answering the user who ran the command. *Not built* -- it needs a message
  component and a second interaction handler, and it is one of the two reasons
  phase 3 is held back.
- **Ephemeral replies** for anything that is nobody else's business. *Built*:
  every refusal is ephemeral. The server password appears in no reply or
  embed, and the join address only if *Show join address* is on.

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

One related trap, now larger: the whole `discord` block lives in
`instance.json`, so an exported instance carries a guild *and* a channel to
another host, where it would start announcing into a Discord that host's
operator cannot see. It is cleared on import, the way identity and port already
are. Clone keeps it — same host, same people.

### Inviting the bot

Settings should build the OAuth2 install URL once connected, from the
application id in the gateway's READY payload: scopes `bot` and
`applications.commands`, permissions limited to **View Channel**, **Send
Messages**, **Embed Links** (plus **Read Message History** if the sticky status
message is enabled). "Here is the link, click it, pick the channel" is a much
better answer than a paragraph telling someone to go build an application.

Commands register guild-scoped (`PUT /applications/{app}/guilds/{guild}/commands`),
once per guild that has an instance mapped to it. That takes effect immediately,
where global registration can take an hour — and it is what lets two Discords
get two different menus.

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

The smoke suite needs no network and no Steam download, and that did not
change. `VHSM_DISCORD_API_BASE` points the whole integration at
`tools/fake_discord.py`, which serves a stub gateway and REST surface on
loopback -- exactly as `tools/fake_valheim_server.py` stands in for the real
binary. It records what the bot said and gives the suite a door of its own
(`/_test/dispatch`, `/_test/drop`, `/_test/reject-gateway`) to put events and
failures in front of it.

What is covered:

- identify, command registration on READY, reconnect after a dropped socket,
  and stopping rather than retrying on `4004` -- including a token revoked
  while the bot is running, which is the failure most likely to happen for
  real
- every read command, its refusals, and the `server` argument's autocomplete
- the gate at each of its gates: guild, channel, read role, and the control
  tier -- including that an empty control allowlist means nobody while an
  empty *read* allowlist means anyone, which is the asymmetry worth a test
- cooldowns, per user and per server
- token handling: environment over file, `0600`, a pasted `Bot "..."` cleaned
  up, and never echoed back
- event derivation from synthetic samples, which is where the conservatism
  gets checked: first sight and the start-up window are silent, reachability
  must agree three times, and each notice, release and restart-pending state
  is announced once
- channel routing: the instance's own channel, the fallback to the default,
  and silence for an instance that never opted in
- a channel Discord refuses, reported on the Settings page rather than
  crashing the bot or stopping the manager

One seam is deliberately tested by hand rather than in the suite: driving a
real server start through to a real "ready" announcement. The suite asserts the
watcher's logic against synthetic snapshots instead, because a real start/stop
cycle is twenty-odd seconds of test time for one assertion the unit checks
already make.

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

1. **Transport** -- *done.* Config, token storage, the gateway and REST
   clients, connection state and the invite link in Settings, and the read
   commands. The hand-rolled gateway was the open question and it came out
   fine: identify, resume, heartbeat-ack timeouts, backoff and the final close
   codes are about 350 lines, and `tools/fake_discord.py` exercises all of it
   offline.
2. **Announcements** -- *done.* The event bus, the snapshot watcher, the
   per-kind switches, and the flapping and boot-burst rules above.
2b. **Everything per instance** -- *done, after the fact.* The guild, the
   channel, the switches, the command toggle, the permissions and the address
   setting all moved from the manager onto the instance, and the guild became
   the scope of command visibility. The manager keeps the token and an on/off
   switch. A one-time migration moves an existing setup down, so an upgrade
   does not silently stop a working bot.
3. **Control** -- *not done, on purpose.* The `control` tier exists in the
   command table, `check_allowed` enforces it (guild, then channel, then role
   or user, with an empty allowlist meaning nobody), `Cooldowns` is written,
   `submit()` takes an `actor` that reaches the log and the bus, and
   `definitions()` leaves control commands unregistered while they are
   switched off -- so they do not appear in Discord's menu until they can
   actually be used. All of that is covered by tests. What is missing is the
   handlers and the confirmation button.
4. **Extras** -- partly done. `/vh mods`, `/vh player`, autocomplete and death
   announcements landed with phases 1-2. The sticky status message did not.

What phase 2b cost, and is worth saying plainly: there are no shared defaults.
Every instance's switches are its own, which is the point, but setting up the
fifth server in the same Discord means ticking the same boxes a fifth time. A
"copy settings from another server" button, or a set of defaults applied to new
instances, is the obvious answer and was left out rather than guessed at.

### Adding a control command

The extension point is deliberate. One entry in `COMMANDS`:

```python
"restart": Command(
    name="restart", description="Restart a server", tier="control",
    handler=_restart, takes_server=True, defer=True,
),
```

and a handler that resolves the instance and hands it to the manager with the
caller's name attached:

```python
async def _restart(ctx: CommandContext) -> dict[str, Any]:
    record = _resolve(ctx)
    ctx.manager.submit(record.config.id, "restart", actor=ctx.actor())
    return _message(f"Restarting **{record.config.name}**...")
```

The gate, the cooldown and the audit entry all happen before the handler is
reached, because the command's tier is `"control"`. The completion needs no
polling either: the status transition is already announced.

Two things still to build for it, and the reason it is not done yet:

- **Confirmation when players are online.** A `stop` that disconnects six
  people should ask first. That is a message carrying a button, and a
  `MESSAGE_COMPONENT` interaction coming back, whose handler has to re-check
  that the clicking user is the one who ran the command *and* still passes the
  gate. Neither the component nor that second check exists yet.
- **Showing the audit trail in the GUI.** The `LIFECYCLE` events carry the
  actor and reach the log, but nothing renders them. Once something other than
  the GUI can stop a server, "who stopped it" deserves a better answer than
  `grep`.
