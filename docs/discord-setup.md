# Setting up the Discord bot

A walk-through from an empty Discord server to a bot that announces what your
Valheim servers are doing and answers questions about them. About ten minutes,
most of it clicking around Discord's website.

**Each server is configured separately, including which Discord it belongs to.**
One bot account serves the whole manager, but a public server can post to a
community Discord while a modded test server lives in a guild of three friends,
with different announcements, different channels and different permissions.
Neither can see the other.

What it needs:

- A Discord server you can manage (you need **Manage Server** to add a bot).
- Five minutes in Discord's Developer Portal, which is free and needs no
  review or approval for a bot used privately.
- No open ports, no domain name, no certificate. The manager dials out to
  Discord; Discord never connects to you. This works on a box behind NAT with
  the GUI still bound to `127.0.0.1`.

---

## 1. Create the application and its bot

1. Go to <https://discord.com/developers/applications> and sign in.
2. **New Application**, top right. Name it whatever you want the bot to be
   called — this becomes its display name in your servers. Accept the terms.
3. In the left sidebar, open **Bot**.
4. Check these, which are not about permissions but about who may add it:
   - **Public Bot** — turn this **off** unless you want other people able to
     add your bot to their servers. Off is right for a private game server,
     and does not stop *you* adding it to as many of your own as you like.
   - **Requires OAuth2 Code Grant** — must be **off**. If it is on, the invite
     link in step 4 fails with an error about a code grant.
5. Leave all three **Privileged Gateway Intents** (Presence, Server Members,
   Message Content) **off**. The bot uses slash commands, which need no
   intents at all. Turning them on gains nothing and is one more thing Discord
   can disable later.

## 2. Get the token

Still on the **Bot** page, under the bot's username:

1. Click **Reset Token**, confirm, and copy what appears.
2. **Copy it now.** Discord shows a token exactly once; if you lose it you
   reset it again, which invalidates the old one.

A token is a password for the bot account. Treat it like one: do not paste it
into a channel, a screenshot, or a git repository. If it leaks, come back here
and **Reset Token** — that is all it takes to make the leaked one useless.

There is **one token** however many Discord servers you use. A bot account can
be invited to any number of them, so "a separate Discord per Valheim server"
means separate *server IDs*, which you set per instance in step 3 — not separate
bots.

Two ways to give it to the manager. Either is fine:

**a) Through the GUI** (simplest). Settings → *Discord bot* → **Bot token** →
paste → **Save token**. It is written to `<data root>/discord_token` with mode
`0600` and is never shown in the GUI again.

**b) Through the environment** (better if you already manage secrets). Set
`VHSM_DISCORD_TOKEN`, which wins over the stored file, and the token never
touches your data volume at all. In `docker-compose.yaml`:

```yaml
    environment:
      VHSM_DISCORD_TOKEN: "your-bot-token"
```

Then recreate the container. The Settings page will show the token as coming
*from the environment* and will not offer to change it there.

Finally, tick **Run the Discord bot** and **Save**. The **Connection** row
should read **connected** within a second or two. That is all there is at the
manager level — everything else belongs to a server.

If it says something else, the panel prints the reason. Jump to
[When it does not work](#when-it-does-not-work).

## 3. Set up your first server

You need two IDs from Discord first.

1. In Discord (the app, not the portal): **User Settings → Advanced → Developer
   Mode**, on. This adds *Copy ID* to right-click menus and is how you get
   every ID below.
2. Right-click the Discord server in the server list → **Copy Server ID**.
3. Right-click the channel you want announcements in → **Copy Channel ID**.

Now open the Valheim server's own page in the manager and expand the **Discord**
section:

| Field | What to put |
|---|---|
| **Use Discord for this server** | tick |
| **Discord server (guild) ID** | the server ID from step 2 |
| **Channel ID** | the channel ID from step 3 |
| **Answer slash commands about this server** | tick, unless you only want announcements |
| **Include this server's join address** | your call — see the note below |
| **Announce** | tick what you want to hear about; see step 5 |

**Save Discord settings.**

> **Include this server's join address** controls whether `host:port` appears in
> this server's announcements and in `/vh status` for it. On a public server it
> is already in the Valheim server browser, so this changes nothing. On a
> private one it means anybody who can read the channel can join. The server
> password is never posted either way.

Repeat this for each Valheim server, with whatever Discord server and channel
each one should use. Two servers can share a guild and a channel, share a guild
but use different channels, or live in entirely separate Discords.

Settings → *Discord bot* has a **Per-server settings** table showing where each
one posts, so you can see the whole picture without opening them one at a time.

## 4. Invite the bot to each Discord server

The bot is connected but is not in any of your Discord servers yet, so nothing
has happened in the channels.

Once connected, the Settings panel shows an **Invite this bot to a server**
button. It asks for exactly three permissions — View Channel, Send Messages,
Embed Links — and nothing else. Click it, pick the Discord server, authorise.
**Use the same link again for each Discord server** you configured in step 3.

(If you would rather build the link yourself, take the **Application ID** from
the portal's *General Information* page and use:
`https://discord.com/oauth2/authorize?client_id=APPLICATION_ID&permissions=19456&scope=bot%20applications.commands`)

Then — and this part matters — go back to each Valheim server's **Discord**
section and click **Save Discord settings** once more. The bot tried to register
its slash commands when it connected, which was before it was allowed into your
Discord, so that attempt failed. Saving registers them again now that it is in.

Each Discord server gets its own `/vh` menu, listing only what is usable there.
They are registered to that one guild, so they appear immediately rather than
taking up to an hour the way global commands do. If `/vh` does not show up,
press **Ctrl+R** (**Cmd+R** on a Mac) to reload the Discord client; it caches
the list.

## 5. Check that it works

Two checks, in order:

1. On the Valheim server's page, in the **Discord** section, **Send a test
   message**. A line naming that server should appear in its channel. If this
   works, the token, that channel's ID and the bot's permissions in that
   Discord are all correct.
2. In Discord, type `/vh servers`. You should get the servers configured for
   *that* Discord, and no others. Try `/vh status` too.

If the test message works but commands do nothing, it is almost always the
command registration — repeat the Save from step 4.

## 6. Choose what each server announces

The **Announce** switches are on each server's own page, so a busy public server
can stay quiet about joins while a server of four friends says everything.

On by default:

- server starting, running or stopped; up and accepting players; crashed
- a recognised failure in the console
- a mod with a newer version; mods changed so a restart is needed; what the
  scheduled mod check found
- a newer Valheim build published, and the server files being updated
- reachable or unreachable from outside

Off by default, because they are the noisy ones:

- a player joined / left / died
- an automatic world snapshot was taken
- the manager started or stopped
- somebody asked for a start, stop or restart

Player joins and leaves are the ones people most often want and most often
regret: on a busy evening it is two lines per player. Try it for a week.

A few events are about the manager rather than one server — a new Valheim build
being published, the manager restarting. Those go to **every channel that asked
for them, once each**: two servers sharing a channel get one message, two
servers in two Discords get one each, and a server with that switch off gets
nothing.

## 7. Narrow who can use it (optional)

By default anybody who can see a channel the bot is in can run the read-only
commands about the servers in that Discord, which is Discord's own permission
model and is usually what you want — the commands only report things most of
which are in the public server browser anyway.

To tighten it, on each Valheim server's page:

- **Command channels** — one channel ID per line. Commands about *this* server
  are only answered in these. Blank means any channel the bot can see in its
  Discord.
- **Roles allowed to ask** — one role ID per line (right-click a role in
  **Server Settings → Roles** → Copy Role ID). Blank means anyone who can see
  an allowed channel.

Because these are per server, you can have one Valheim server answerable to
everybody in a Discord and another in the same Discord answerable only to a
`@staff` role.

**A Discord only ever sees its own servers.** A command from one guild cannot
list, autocomplete or address a server configured for another — not by name, not
by instance id. A guild with nothing mapped to it is told only "no servers here
are set up to answer you", which is also what somebody without the right role
is told, so the refusal reveals nothing either way.

**There is no command that can start, stop or restart a server.** That is not
an oversight — see [the design note](discord-bot.md) for why it is being done
separately.

---

## The commands

| Command | What it answers |
|---|---|
| `/vh servers` | the servers in this Discord, one line each |
| `/vh status [server]` | status, uptime, version, players, join address, mods |
| `/vh players [server]` | who is on right now, and for how long |
| `/vh mods [server]` | which mods it runs, and what has an update |
| `/vh player <name>` | one player's stats, online or not |
| `/vh version` | manager, server and Steam build versions |

`[server]` can be left out when only one server is configured for that Discord.
Otherwise it autocompletes from the ones visible there, and a partial name works
if it matches only one.

`/vh player` reads the roster, so it answers for somebody who left an hour ago
— which is the point of keeping a roster. It only searches the servers visible
in that Discord.

---

## Upgrading from the older, manager-wide setup

Earlier versions had one Discord server, one default channel and one set of
announcement switches for the whole manager. If that is what you had, the first
start after the upgrade moves those settings down onto every server
automatically — the guild, the channel, the switches, the permissions — and
servers that were announcing carry on announcing. You should not have to set
anything up again.

Two things to know:

- The settings are now on each server's page. Settings → *Discord bot* keeps
  only the token and the on/off switch.
- A server that was announcing with no channel of its own used to fall back to
  the manager's default channel. There is no default any more, so if the
  migration found no channel to copy, that server is switched off rather than
  left silently broken. Give it a channel ID and tick it back on.

---

## When it does not work

The **Connection** row and the red box in Settings → *Discord bot* are the
first place to look for anything about the bot account. Anything about one
server is in that server's own **Discord** section.

**"Discord rejected the bot token."**
The token is wrong, or it was reset after you saved it. Go back to the portal,
**Reset Token**, and save the new one. Note that the bot deliberately *stops*
on this rather than reconnecting — there is no point hammering Discord with a
password it has already refused — so after fixing the token you may need to
save it again to start it back up.

**"The bot is not allowed to do that."**
It is in that Discord but cannot post in that channel. Check the channel's own
permissions (**Edit Channel → Permissions**) for the bot's role: it needs
*View Channel*, *Send Messages* and *Embed Links*. A private channel denies
these by default even to a bot that has them server-wide. The message names the
channel it was refused, which tells you which server's settings to look at.

**"Discord does not know that id."**
A channel or server ID is wrong. They are long strings of digits — if what you
pasted has a `#` or a `<` in it, you copied the channel *mention* rather than
its ID. Developer Mode must be on for *Copy ID* to appear.

**"This server needs the Discord server (guild) ID it belongs to."**
You ticked *Use Discord for this server* without saying which Discord. The
guild is what scopes everything else, so there is no sensible default for it.

**"Announcements need a channel ID."**
Announcement switches are on but there is nowhere to post them. Give it a
channel, or switch every announcement off to use commands only.

**Connected, but `/vh` does not exist in that Discord.**
Registration failed, most likely because the bot was not in that Discord when
it connected. Click **Save Discord settings** on a server in that guild, then
reload the Discord client with Ctrl+R.

**`/vh servers` says "No servers here are set up to answer you".**
That one sentence covers four causes, deliberately: no server in this manager
names this Discord; the ones that do have *Answer slash commands* off; this
channel is not in their **Command channels**; or you do not have a role in their
**Roles allowed to ask**. Check them in that order.

**Connected, commands work, but nothing is ever announced.**
Two switches have to line up on that server's page: *Use Discord for this
server*, and the particular event under **Announce**. The counter on the
Settings panel shows how many announcements have been sent in total.

**It announced nothing when I restarted the manager.**
Working as intended. The bot announces *changes*, and the state it sees in its
first few seconds is what was already true — a manager restart would otherwise
read as every server stopping and starting at once.

**A server shows as unreachable, then reachable, repeatedly.**
The reachability probe leaves your host and some routers will not loop traffic
back to themselves, so it can report a working server as unreachable. A change
is only announced after three consecutive samples agree, but if it flaps for
real, untick **Reachable or unreachable from outside** for that server.

### Reading the log

The manager logs everything the bot does under the `vhsm.discord` logger. In
Docker:

```bash
docker logs -f vhsm 2>&1 | grep discord
```

A healthy start looks like this — `started` first, because connecting is a
round trip that finishes a moment later, then one registration line per Discord
server:

```
discord bot started
discord: connected as your-bot-name (application 1234...)
discord: commands registered in guild 5678...
discord: commands registered in guild 9012...
```

A reconnect, which is normal and happens whenever Discord moves you between
gateways, looks like:

```
discord gateway lost (connection closed (1001)); retrying
discord: session resumed
```

---

## What it will not do

Worth knowing before you go looking for it:

- **No chat relay, in either direction.** Valheim sends chat as a message
  between clients that the dedicated server only passes along — it never
  reaches the console, which is the only window the manager has into the game.
  Reading or posting in-game chat needs a server-side mod.
- **No start/stop/restart from Discord.** Not yet, deliberately.
- **No player ping.** Valheim does not expose it anywhere, so the bot cannot
  report it and will not invent it.
- **No outbox.** An announcement that happens while the bot is reconnecting, or
  while Discord is refusing the channel, is dropped rather than queued. The
  Settings panel says when posting last failed.
- **No shared defaults.** Every server's switches are its own, so setting up the
  fifth one means ticking the same boxes a fifth time. Worth revisiting if
  anybody ends up with a lot of servers in one Discord.

## Where things are kept

```
<data root>/
  discord.json       whether the bot runs. Not the token, and not the settings.
  discord_token      the token, mode 0600, unless VHSM_DISCORD_TOKEN is set
  instances/<id>/
    instance.json    this server's Discord settings, with the rest of its config
```

Because the settings live in `instance.json`, they travel with the instance —
and are **cleared when an instance is imported from an archive**, since they
name a Discord that the importing operator can neither see nor be assumed to
want posted to.
