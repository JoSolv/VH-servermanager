# Design note: a built-in Discord bot

Status: **a player channel per server.** One bot account; each instance names a
Discord channel, and that channel is its identity -- a command typed there is
about that server, takes no arguments, and can reach nothing else. The seven
announcements and three commands on offer are written for that server's
players. Everything operator-facing -- crashes, mod releases, build ids, who
asked for a restart, `/vh players` -- is kept, switchable and tested, and goes
nowhere until there is a second, admin channel. Control commands are
deliberately absent too: their permission tier is built and enforced, but no
command uses it. See [Phases](#phases) for what landed and what did not.

This note is the design, kept as the record of *why* it is shaped this way.
Where the implementation settled somewhere different from the proposal, the
text says so rather than being quietly corrected.

**To actually set the bot up**, see [`discord-setup.md`](discord-setup.md).
This file is about the reasoning, not the clicking.

## Two audiences, one of which exists

The thing being built is two channels per server, not one:

- a **player channel** -- the one that exists -- where the people who play on
  that server are told it is up, it is going down, it came back with new mods,
  somebody joined; and where they can ask three questions about it
- an **admin channel**, planned, where the operator gets the rest: crashes,
  console failures, mod releases, build ids, reachability, who asked for a
  restart, and eventually the commands that start and stop things

Every event kind and every command carries an `audience`, and only `player`
ones are offered or posted. That is the whole of the first half of the admin
channel: when it arrives, it is a second channel id per instance and a routing
change, not a rethink. Nothing about the admin side was deleted to get here --
the switches are all still stored and still saved, just not rendered, so
turning the channel on later does not mean setting it all up again.

The split also forced a question worth having asked: *what does a player
actually want to be told?* Not "the status changed to stopping" -- that is a
state machine talking. They want "the server is going down", and above all they
do not want three messages at 4am because a scheduled update bounced it. See
[What players are told](#what-players-are-told).

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

## What players are told

Seven things, and the interesting part is which of them are *held back*.

| Switch | Fires on | Default |
|---|---|---|
| Server is online and joinable | the server becoming reachable -- holding its port, or its query socket answering -- not the process existing | on |
| Server is shutting down | a stop somebody asked for | on |
| Server is restarting | a restart somebody asked for | on |
| Server came back with updated mods | it came up running a different mod set than last time | on |
| Server was updated | it came up on a newer build than last time | on |
| A player joined | | off |
| A player left | | off |

### A maintenance restart is one piece of news, not four

When the manager takes a server down for a scheduled server update, or restarts
it because a mod update installed something, the stop, the restart and the
coming-back are all suppressed. A player woken by three messages at 4am learns
nothing they can act on; what they want is what changed, and they want it when
the server is back.

So the instance carries a `maintenance` string while the manager is cycling it,
and the watcher checks it at the transition. The flag's *lifetime* is the part
that took a second attempt: clearing it when `restart()` returns is too early,
because `start()` returns as soon as the process is launched and the server is
not reachable for several seconds yet -- so the online announcement fired
anyway. It is cleared instead when the server is next seen reachable, which is
exactly the moment the suppression has to cover.

That in turn has an ordering requirement: the suppression reads the sampling
snapshot, so the flag must still be set *in the snapshot for the tick that sees
it come back*. `sample_once` therefore captures each snapshot before the call
that may clear the flag. Get that the wrong way round and the feature silently
stops working, which is why it is one of the tested cases.

A restart somebody asks for by hand is not maintenance and is announced
normally.

### "What changed" is a comparison, not a notification

The mod and update announcements are not emitted where the install happens.
They compare what the server is **running** against what it was running the
last time it came up:

- a mod installed but not yet loaded is not news -- `restart_pending` says the
  files moved and this process has not read them, so the comparison waits
- a build installed while the server was down is not news until it is up on it
- the first sighting of an instance only records the baseline, so restarting
  the manager does not report the whole mod list as new

That one rule covers installs, updates, enables, disables and removals without
instrumenting any of those paths, and it is what makes "and the instance is
actually running said mods" true rather than hoped for. The comparison re-reads
the manifest only when `ModState.changed_at` has moved, so it costs nothing on
a quiet tick.

The build comparison keys on the Steam build id, because that changes
reliably, but *reports* the Valheim version from the server's own console
banner -- which is the thing a player has to match to join. The build id is
operator business and stays out of the channel.

### What is not told, and where it went

Crashes, console failures, mod releases, mods-need-a-restart, scheduled check
results, published builds, reachability, snapshots, manager start-up and
lifecycle requests are all `audience="admin"`. They are published on the bus,
switchable per instance, and dropped by the announcer. One guard in
`channels_for` does it, and lifting that guard is most of what the admin
channel needs.

The deliberate gap: **a crash tells the players nothing.** The server goes
quiet and says "online" when it comes back. That follows from the list above
and is the right default -- a crash loop would otherwise spam the channel --
but it is a real gap and worth revisiting if anybody misses it.

## One voice

Everything the bot says goes through `vhsm/discord/render.py`, which holds the
rules rather than leaving each call site to decide. They exist because the
first pass had the bot speaking four dialects at once: an embed title with
`**bold**` in it (Discord renders that literally), a status field reading
`running` next to an announcement reading "is online", the server named in both
a title and a footer, and a different emoji per message.

- **One mark vocabulary**, five glyphs with one meaning each: green for
  up-or-here, black for gone, yellow for on-its-way, a package for mods, an
  arrow for a version. Nothing else, so a glance down a channel is legible
  without reading any of it.
- **Title is the subject, footer is the context.** An announcement is about the
  server, so the server is in its title. A command reply is about what was
  asked -- the mods, the players, one player -- so that is the title, and the
  server plus any count goes in the footer.
- **Nothing is said twice.** The *online* announcement's title already says the
  server is up, so its card omits the status row; `/vh status` has a neutral
  title and keeps it.
- **No markdown in embed titles**, because Discord shows the asterisks.
- **Bold the subject of the sentence** in plain messages: *"**Freyja** joined
  Midgard"*, *"**Midgard** is shutting down"*.
- **One line is a message, a set of facts is an embed.** A channel of boxes for
  joins and leaves reads like an alarm going off.
- **Player words, not process words.** `status_label()` is the single place
  where `running` becomes "Online" -- and it answers "Online" only when the
  server is actually joinable, so a process that is up but still loading the
  world reads "Starting up" rather than lying. A crash reads "Offline", because
  to a player it is, and "crashed" in their channel invites questions nobody in
  it can answer.
- **No operator detail in a player reply.** `/vh status` used to carry the
  console notices, the exit code and a mod-update count. All three are things
  only the operator can act on, and all three are gone.

The payoff beyond tidiness: the *online* announcement and `/vh status` are
**the same card**, built by `render.server_fields()`. Being told and asking do
not merely cover the same ground, they look alike -- and a change to one cannot
drift from the other.

## Where the announcements come from

The manager already produces nearly all of this; very little of it is new
monitoring. A small `vhsm/events.py` -- an `EventBus` shaped like the existing
`Hub`, with typed events instead of snapshot JSON -- is fed from the places
below. The Discord bot is one subscriber; a webhook or email sink later would
be another.

| Announcement | Where it comes from | Audience |
|---|---|---|
| online | `listening` or `query_answered`, the first time either is true in a run | player |
| shutting down / restarting | the `operation` the manager put on the instance | player |
| came back with updated mods | the enabled mod set, compared against the last one seen running | player |
| was updated | the installed build id, compared the same way | player |
| joined / left | `PlayerTracker`'s resolved `named` and `disconnect` events | player |
| starting / running / stopped | `Supervisor.status` transitions | admin |
| ready | the same signals as *online*, worded for an operator | admin |
| crashed | `supervisor.exit_code`, `supervisor.last_error` | admin |
| recognised console failures | `supervisor.notices`, already keyed for change detection | admin |
| a mod has a newer version | `record.mod_notices` | admin |
| mods changed but not loaded | `record.restart_pending` | admin |
| scheduled mod check result | `ModUpdateRun.summary()` | admin |
| a build was published / installed | `check_update()`, `run_update()` | admin |
| reachable / unreachable | `record.reachable`, after three agreeing samples | admin |
| snapshot taken | `_auto_snapshot()` | admin |
| manager up / down | app lifespan | admin |
| a lifecycle action, and who asked | `submit(actor=...)` | admin |

Routing happens at the far end, in the announcer, and every destination belongs
to an instance. An event about one instance goes to that instance's channel if
that instance announces that kind of thing *and* the kind is player-facing. An
event about no instance fans out to each distinct channel among the instances
that want it -- which, now that channels are unique per instance, is one per
server.

Two things are worth getting right rather than shipping naively:

- **Flapping.** `reachable` is a UDP round trip and will fail transiently.
  Announce only after N consecutive samples agree, not on every change.
- **A burst at boot.** Nothing should re-announce state that merely became
  visible again because the manager restarted. Transitions are only announced
  when both sides were observed *in this run*, with a short quiet period after
  startup -- the same "recognised in this run" rule `IssueWatcher` already uses.

### Sources needing a small change

- `PlayerTracker` took a single `on_event` callback and the manager had already
  spent it on `roster.observe`. It fans out to two listeners now.
- `InstanceManager.submit()` takes an optional `actor` (default `"web"`), so an
  announcement can say *restarted by @someone* and the audit trail has a name
  in it. This is the same hook the audit log needs, so it pays twice.

## Command surface

Namespaced under one command so it never collides with another bot, and every
one of them is about the server whose channel it was typed in. None takes a
server argument; none reports anything beyond that server.

**Offered -- the three a player has questions for**

| Command | Answers with |
|---|---|
| `/vh status` | is it up, uptime, version, players *n*/*max*, join address, mod count |
| `/vh mods` | the names and versions it is running, and nothing else -- the list to match in r2modman |
| `/vh player [name]` | with a name, that player's sessions, time played and last seen; without one, everybody who has ever played here, online first |

`/vh player` collapses two questions into one command, because "who plays on
this server" and "is Freyja on right now" are the same question at two widths.
It reads the roster, so it answers for somebody who left an hour ago -- which
is the whole point of keeping a roster.

`/vh mods` deliberately drops what the operator-facing version showed: which
mods have an update, and whether the server needs restarting to pick one up.
Neither is actionable by a player, and both are on the instance page.

**Stored for the admin channel**

| Command | Why it waits |
|---|---|
| `/vh players` | who is *connected* right now. `/vh player` with no name is the player-facing version of this; a second command listing a subset is one more menu entry for no extra answer |

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

A command marked `audience="admin"` is not merely unregistered -- `dispatch`
refuses it too, so a client holding a stale menu cannot reach one.

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
  while the bot is running, which is the failure most likely to happen for real
- the three offered commands, answering with no argument for the channel they
  were typed in, and a stray `server:` value being ignored rather than obeyed
- that only those three are registered, and that a command kept for the admin
  channel is refused even when asked for directly
- **two servers in two channels, neither reachable from the other's channel**,
  a channel id from the wrong Discord not matching, and a channel with no
  server behind it getting one sentence
- that no command lists the servers or reports a manager or Steam version
- the guild being looked up from the channel rather than typed, a channel the
  bot cannot see being reported and answering no commands until fixed, and two
  servers sharing a channel being refused
- **what players are told about a restart**: a stop somebody asked for is
  announced, a restart says *restarting* rather than *shutting down*, and the
  server coming back says *online*
- **and what they are not**: every stop, restart and start-up of a maintenance
  cycle is suppressed, including the start-up a tick after the window closes,
  while a plain restart straight afterwards is announced again
- that every admin-audience kind is published and switchable but never posted
  to a player channel, and that an unknown kind defaults to admin rather than
  leaking
- that saving the instance page leaves the switches it does not render alone,
  and cannot set them either
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
- per-channel announcement routing, and silence for a server that never opted in
- a channel Discord refuses, reported on the Settings page rather than crashing
  the bot or stopping the manager
- the one-time move of the old manager-wide settings down onto the instances,
  including that the old default channel can only go to one of them and the
  rest are switched off rather than left broken

Two seams are deliberately tested by hand rather than in the suite: driving a
real server start through to a real "online" announcement, and a real scheduled
update end to end. The suite asserts the watcher's logic against synthetic
snapshots instead, because a real start/stop cycle is twenty-odd seconds of
test time for assertions the unit checks already make.

Two lessons from building this, both worth keeping:

- Half the command assertions are of the form "the *other* server is not
  mentioned", and those pass against a reply that never arrived. A broken
  handler once looked like correct scoping across a dozen checks. `ask()` now
  reports a missing reply as its own failure.
- The watcher publishes both audiences and it is the *announcer* that filters,
  so a test that drains the bus and asserts "nothing was said to players" has
  to filter too, or it fails on an admin event it was never about. That cost a
  round of false failures.

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
- **No admin channel.** Everything meant for one is written, switchable and
  tested; nothing routes to it. See phase 5.
- **A crash says nothing to players.** By design, per the list above, but worth
  naming as a gap rather than leaving it to be discovered.

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
2d. **Written for players** -- *done, after that again.* Seven player-facing
   announcements replaced the operator-facing ones on screen, with the stops
   and starts of a maintenance restart suppressed in favour of one message
   about what changed; `/vh players` moved to the stored set and `/vh player`
   grew an optional name; `/vh mods` lost everything a player cannot act on.
   Every kind and command now carries an `audience`, which is the groundwork
   for the admin channel.
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
4. **Extras** -- partly done. `/vh mods` and `/vh player` landed; autocomplete
   and death announcements landed and were then removed as the scope narrowed.
   The sticky status message did not.
5. **The admin channel** -- *not started, but designed for.* A second channel
   id per instance, and `channels_for` routing admin-audience events to it
   instead of dropping them. The switches, the handlers and `/vh players` are
   all in place; what is missing is the field, the routing and a decision about
   whether the admin channel should also carry the player events (probably
   not -- that is what the player channel is for).

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
- **Half the switches on disk do nothing yet.** The admin-audience ones are
  stored, saved and switchable, and go nowhere. That is the deliberate price of
  not having to set them up again when the admin channel lands, but a config
  file whose values are partly inert is a thing to remember when reading one.

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
