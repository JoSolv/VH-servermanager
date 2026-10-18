"""The optional built-in Discord bot.

One bot for the manager, with announcements routed per instance. Everything
here is inert until an operator configures it: see :mod:`vhsm.discord.bot`.

The dependency runs one way. This package reads the manager; the manager knows
only about :class:`~vhsm.events.EventBus`, so a Discord outage, a bad token or
this package failing to import can never stop a server from being managed.
"""
