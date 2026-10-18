# Setting up the Discord bot

A walk-through from an empty Discord server to a bot that announces what your
Valheim servers are doing and answers questions about them. About ten minutes,
most of it clicking around Discord's website.

The bot is optional and does nothing until you do all of this. What it needs:

- A Discord server you can manage (you need **Manage Server** to add a bot).
- Five minutes in Discord's Developer Portal, which is free and needs no
  review or approval for a bot used in one server.
- No open ports, no domain name, no certificate. The manager dials out to
  Discord; Discord never connects to you. This works on a box behind NAT with
  the GUI still bound to `127.0.0.1`.

---

## 1. Create the application and its bot

1. Go to <https://discord.com/developers/applications> and sign in.
2. **New Application**, top right. Name it whatever you want the bot to be
   called — this becomes its display name in your server. Accept the terms.
3. In the left sidebar, open **Bot**.
4. Check these, which are not about permissions but about who may add it:
   - **Public Bot** — turn this **off** unless you want other people able to
     add your bot to their servers. Off is right for a private game server.
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

## 3. Switch the bot on

You need your Discord server's ID first.

1. In Discord (the app, not the portal): **User Settings → Advanced → Developer
   Mode**, on. This adds *Copy ID* to right-click menus and is how you get
   every ID below.
2. Right-click your server in the server list → **Copy Server ID**.
3. Right-click the channel you want announcements in → **Copy Channel ID**.

Now in the manager, Settings → *Discord bot*:

| Field | What to put |
|---|---|
| **Run the Discord bot** | tick |
| **Discord server (guild) ID** | the server ID from step 2 |
| **Default channel ID** | the channel ID from step 3 |
| **Answer slash commands** | tick, unless you only want announcements |
| **Include the join address** | your call — see the note below |

Click **Save Discord settings**. Within a second or two the **Connection** row
at the top of the panel should read **connected**, with the bot's username
beside it.

If it says something else, the panel prints the reason. Jump to
[When it does not work](#when-it-does-not-work).

> **Include the join address** controls whether `host:port` appears in status
> announcements and `/vh status`. On a public server it is already in the
> Valheim server browser, so this changes nothing. On a private one it means
> anybody who can read the channel can join. Your call; the server password is
> never posted either way.

## 4. Invite the bot to your server

The bot is connected to Discord but is not in your server yet, so nothing has
happened in the channel.

Once connected, the panel shows an **Invite this bot to a server** button. It
asks for exactly three permissions — View Channel, Send Messages, Embed Links —
and nothing else. Click it, pick your server, authorise.

(If you would rather build the link yourself, take the **Application ID** from
the portal's *General Information* page and use:
`https://discord.com/oauth2/authorize?client_id=APPLICATION_ID&permissions=19456&scope=bot%20applications.commands`)

Then — and this part matters — go back to Settings and click **Save Discord
settings** once more. The bot tried to register its slash commands when it
connected, which was before it was allowed into your server, so that attempt
failed. Saving registers them again now that it is in.

Slash commands are registered to your one server, so they appear immediately
rather than taking up to an hour the way global ones do. If `/vh` does not
show up in Discord's command list, press **Ctrl+R** (**Cmd+R** on a Mac) to
reload the Discord client; it caches the list.

## 5. Check that it works

Two checks, in order:

1. In Settings, **Send a test message**. A line should appear in your default
   channel. If this works, the token, the channel ID and the bot's permissions
   are all correct.
2. In Discord, type `/vh servers`. You should get a list of your instances with
   their status. Try `/vh status` too.

If the test message works but commands do nothing, it is almost always the
command registration — repeat the Save from step 4.

## 6. Choose what gets announced, and where

Nothing is announced yet. Announcements are opt-in **per instance**, because
the common case is one server posting to one channel and another staying quiet.

**Per instance:** open the instance's page → the **Discord** section → tick
**Announce this server's events to Discord**, and optionally give it a
**Channel ID** of its own. Leave the channel blank to use the default channel
from step 3. Save.

**What kinds of thing:** back in Settings → *Discord bot* → **Announce**. These
are manager-wide — they decide *what* is worth saying, while each instance
decides *where* it goes.

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

## 7. Narrow who can use it (optional)

By default anybody who can see a channel the bot is in can run the read-only
commands, which is Discord's own permission model and is usually what you want
— the commands only report things most of which are in the public server
browser anyway.

To tighten it, in Settings → *Discord bot*:

- **Command channels** — one channel ID per line. Commands are only answered
  in these. Blank means any channel the bot can see.
- **Roles allowed to ask** — one role ID per line (right-click a role in
  **Server Settings → Roles** → Copy Role ID). Blank means anyone who can see
  an allowed channel.

The bot also ignores anything outside the one server you configured, so a
leaked token cannot be used to point it at someone else's Discord.

**There is no command that can start, stop or restart a server.** That is not
an oversight — see [the design note](discord-bot.md) for why it is being done
separately.

---

## The commands

| Command | What it answers |
|---|---|
| `/vh servers` | every instance, one line each: status, players, version |
| `/vh status [server]` | status, uptime, version, players, join address, mods |
| `/vh players [server]` | who is on right now, and for how long |
| `/vh mods [server]` | which mods it runs, and what has an update |
| `/vh player <name>` | one player's stats, online or not |
| `/vh version` | manager, server and Steam build versions |

`[server]` can be left out when you only run one instance. Otherwise it
autocompletes from your instance names, and a partial name works if it matches
only one.

`/vh player` reads the roster, so it answers for somebody who left an hour ago
— which is the point of keeping a roster.

---

## When it does not work

The **Connection** row and the red box under it in Settings are the first place
to look. The common ones:

**"Discord rejected the bot token."**
The token is wrong, or it was reset after you saved it. Go back to the portal,
**Reset Token**, and save the new one. Note that the bot deliberately *stops*
on this rather than reconnecting — there is no point hammering Discord with a
password it has already refused — so after fixing the token you may need to
save it again to start it back up.

**"The bot is not allowed to do that."**
It is in your server but cannot post in that channel. Check the channel's own
permissions (**Edit Channel → Permissions**) for the bot's role: it needs
*View Channel*, *Send Messages* and *Embed Links*. A private channel denies
these by default even to a bot that has them server-wide.

**"Discord does not know that id."**
A channel or server ID is wrong. They are long strings of digits — if what you
pasted has a `#` or a `<` in it, you copied the channel *mention* rather than
its ID. Developer Mode must be on for *Copy ID* to appear.

**Connected, but `/vh` does not exist in Discord.**
Registration failed, most likely because the bot was not in the server when it
connected. Click **Save Discord settings** again, then reload the Discord
client with Ctrl+R.

**Connected, commands work, but nothing is ever announced.**
Three switches have to line up: the instance has *Announce this server's events
to Discord* ticked, that kind of event is ticked under **Announce**, and there
is a channel (the instance's own, or the default). The counter on the Settings
panel shows how many announcements have been sent.

**It announced nothing when I restarted the manager.**
Working as intended. The bot announces *changes*, and the state it sees in its
first few seconds is what was already true — a manager restart would otherwise
read as every server stopping and starting at once.

**A server shows as unreachable, then reachable, repeatedly.**
The reachability probe leaves your host and some routers will not loop traffic
back to themselves, so it can report a working server as unreachable. A change
is only announced after three consecutive samples agree, but if it flaps for
real, turn off **Reachable or unreachable from outside** under Announce.

### Reading the log

The manager logs everything the bot does under the `vhsm.discord` logger. In
Docker:

```bash
docker logs -f vhsm 2>&1 | grep discord
```

A healthy start looks like this — `started` first, because connecting is a
round trip that finishes a moment later:

```
discord bot started
discord: connected as your-bot-name (application 1234...)
discord: commands registered in guild 5678...
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

## Where things are kept

```
<data root>/
  discord.json       the settings from this guide. Not the token.
  discord_token      the token, mode 0600, unless VHSM_DISCORD_TOKEN is set
```

Each instance's own channel lives in its `instance.json`, alongside the rest of
its configuration — and is cleared if that instance is ever imported from an
archive on another host, since it names a channel the new operator cannot see.
