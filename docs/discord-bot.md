# Design note: a built-in Discord bot

Status: **announcements and read-only commands implemented, scoped per
channel.** One bot account; each instance names a Discord channel, and that
channel is its identity -- a command typed there is about that server, takes no
arguments, and can reach nothing else. No command reports anything about the
manager or the other servers. Control commands are deliberately not here: the
permission tier they need is built, enforced and tested, but no command uses it
yet. See [Phases](#phases) at the end for what landed and what did not.

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

### The channel is the identity

The first cut scoped by guild and gave every command a `server` argument. That
was still too wide, and the correction is the one that makes the feature make
sense: **the channel identifies the server.**

An interaction arrives in one channel. At most one instance names that channel
-- the manager refuses to save two that do -- so resolving the target is a
lookup, not a search, and it happens before any handler runs. From there:

- no command takes a server argument, so there is nothing to autocomplete and
  nothing to enumerate for the autocompleter to leak
- `/vh status` in `#midgard` answers for Midgard, full stop
- `/vh player` searches that one server's roster
- a channel with no instance behind it gets "no Valheim server is set up for
  this channel", which is also what somebody without the right role gets, so
  the refusal distinguishes nothing

The guild is still checked, as a second condition: a channel id reused in
another Discord, or left stale after a move, must not match. But it is no
longer something the operator types -- it is looked up from the channel with
`GET /channels/{id}` and stored, because registering commands needs a guild id
before any interaction has arrived. One pasted id per server instead of two,
and a lookup that fails says "the bot cannot see that channel", which is a
better first-run error than silence.

**And nothing manager-level is reachable.** `/vh servers` and `/vh version`
were both removed: the first enumerated the host's servers, the second reported
the manager's build and the shared game install's. The people in a server's
channel are its players, and none of that is theirs. What is left --
`status`, `players`, `mods`, `player` -- is four questions about one server.

Command registration still happens per guild (`PUT
/applications/{app}/guilds/{guild}/commands`), since that is the only
granularity Discord offers, and a guild that stops having any instance has its
commands removed rather than left answering "no server here" forever.

## Command surface

Namespaced under one command so it never collides with another bot, and every
one of them is about the server whose channel it was typed in. None takes a
server argument; none reports anything beyond that server.

**Read**

| Command | Answers with |
|---|---|
| `/vh status` | state, uptime, version, players *n*/*max*, join address, mod count, whether a restart is pending |
| `/vh players` | who is on, character name, time played, platform |
| `/vh mods` | active mods and versions, and what has an update |
| `/vh player <name>` | from the roster: sessions, total time played, last seen -- works for someone who left an hour ago, which is the whole point of the roster |

**Control** -- off until switched on, and separately gated

| Command | Notes |
|---|---|
| `/vh start` | |
| `/vh restart` | |
| `/vh stop` | asks for confirmation with a button when anyone is online; a stop disconnects people |

**Removed on purpose:** `/vh servers`, which listed every instance the host
runs, and `/vh version`, which reported the manager's own build and the shared
game install's. Both answered questions the players in one server's channel
have no business asking. The server's own version is in `/vh status`, where it
belongs.

Control commands reply immediately -- `submit()` returns as soon as the task is
owned by the manager -- and the *completion* arrives through the announcement
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
- **Each instance reachable only from its own channel.** *Built.* An
  interaction resolves to the single instance that names the channel it came
  from, so a token that leaks or an invite into a second Discord reaches at
  most the one server that channel is for — never the host, never the others.
  There is no argument that could name anything else and no command that
  enumerates anything, so there is nothing to probe with.
- **Nothing manager-level in reach.** *Built.* No command reports the manager's
  version, the shared install's build, or that other servers exist. This is a
  narrower surface than "the commands are read-only": a read-only command that
  lists every server on the host still tells a stranger how much there is to
  attack.
- **Role and user allowlists, per instance, separately for read and control.**
  *Built.* By role id and user id. An empty read allowlist means anyone who can
  see the channel, which is Discord's own gate; an empty *control* allowlist
  means nobody, which is the asymmetry that matters and is tested as such. Two
  instances in one Discord can have different answers.
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
(`/_test/dispatch`, `/_test/drop`, `/_test/reject-gateway`,
`/_test/channel-guild`) to put events, failures and channel-to-guild mappings
in front of it.

What is covered:

- identify, command registration on READY, reconnect after a dropped socket,
  and stopping rather than retrying on `4004` -- including a token revoked
  while the bot is running, which is the failure most likely to happen for
  real
- every read command, answering with no argument for the channel it was typed
  in, and a stray `server:` value being ignored rather than obeyed
- **two servers in two channels, neither reachable from the other's channel**,
  a channel id from the wrong Discord not matching, and a channel with no
  server behind it getting one sentence
- that no command lists the servers or reports a manager or Steam version, that
  none takes a server argument, and that nothing needs autocompleting
- the guild being looked up from the channel rather than typed, a channel the
  bot cannot see being reported and answering no commands until fixed, and two
  servers sharing a channel being refused
- the per-instance gate at each of its gates (channel, guild, read role,
  commands switch, and the control tier that is not shipped yet, including that
  an empty control allowlist means nobody and that switching control on for one
  server does not unlock another)
- cooldowns, per user and per server
- token handling: environment over file, `0600`, a pasted `Bot "..."` cleaned
  up, and never echoed back
- event derivation from synthetic samples, which is where the conservatism gets
  checked: first sight and the start-up window are silent, reachability must
  agree three times, and each notice, release and restart-pending state is
  announced once
- per-channel announcement routing, the fan-out of manager-wide events to every
  channel that wants them, and silence for a server that never opted in
- a channel Discord refuses, reported on the Settings page rather than
  crashing the bot or stopping the manager
- the one-time move of the old manager-wide settings down onto the instances,
  including that the old default channel can only go to one of them and the
  rest are switched off rather than left broken

One seam is deliberately tested by hand rather than in the suite: driving a
real server start through to a real "ready" announcement. The suite asserts the
watcher's logic against synthetic snapshots instead, because a real start/stop
cycle is twenty-odd seconds of test time for one assertion the unit checks
already make.

A lesson from building this, worth keeping: half the assertions here are of the
form "the *other* server is not mentioned", and those pass against a reply that
never arrived. A broken handler once looked like correct scoping across a dozen
checks. `ask()` now reports a missing reply as its own failure, so that cannot
happen quietly again.

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
   setting all moved from the manager onto the instance. The manager keeps the
   token and an on/off switch. A one-time migration moves an existing setup
   down, so an upgrade does not silently stop a working bot.
2c. **Scoped per channel, and narrowed** -- *done, after that.* The channel
   became the instance's identity rather than one of its settings: commands
   lost their `server` argument and their autocomplete, `/vh servers` and
   `/vh version` were removed as manager-level, and channels became unique
   across instances. The guild stopped being typed in and is looked up from
   the channel instead. This is the version worth copying if the design is
   ever rebuilt: the first two cuts were both wider than the thing people
   actually want, which is "this channel is my server".
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

What this cost, and is worth saying plainly:

- **No shared defaults.** Every instance's switches are its own, which is the
  point, but setting up the fifth server means ticking the same boxes a fifth
  time. A "copy settings from another server" button, or defaults applied to
  new instances, is the obvious answer and was left out rather than guessed at.
- **One channel per server, strictly.** Two instances cannot post to one
  channel, because the channel is how a command is matched. An operator who
  wanted a single `#servers` channel for everything cannot have it: they would
  have to give up commands, or give one server the channel and the rest none.
  That is the price of commands that need no arguments, and it seems the right
  way round -- but it is a real constraint, not an oversight.

### Adding a control command

The extension point is deliberate. One entry in `COMMANDS`:

```python
"restart": Command(
    name="restart", description="Restart this server", tier="control",
    handler=_restart, defer=True,
),
```

and a handler that hands the channel's instance to the manager with the
caller's name attached:

```python
async def _restart(ctx: CommandContext) -> dict[str, Any]:
    record = ctx.record
    ctx.manager.submit(record.config.id, "restart", actor=ctx.actor())
    return _message(f"Restarting **{record.config.name}**...")
```

`ctx.record` is the instance that owns the channel, resolved before the handler
runs -- so there is nothing to look up and no way to address anything else.

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
