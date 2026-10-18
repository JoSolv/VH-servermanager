"""The HTTP half of the bot: everything it says, and command registration.

Only the gateway needs a websocket. Sending a message, registering the
commands and answering an interaction are ordinary requests, so they go
through the ``httpx`` client this project already depends on.

Rate limits are handled the way Discord documents them: a 429 carries
``retry_after`` and is retried, and the per-route reset headers are read so a
burst of announcements paces itself instead of earning the 429 first.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

log = logging.getLogger("vhsm.discord.rest")

#: v10 is the current stable REST version.
DEFAULT_API_BASE = "https://discord.com/api/v10"
#: Attempts for a request that is rate limited or hits a server-side error.
MAX_ATTEMPTS = 4
#: Never sleep longer than this for a 429. A retry_after beyond it means we
#: are in a long global limit, and the caller is better told than left
#: hanging -- an announcement is not worth stalling a task for a minute.
MAX_RETRY_SLEEP = 10.0
REQUEST_TIMEOUT = 15.0


class DiscordAPIError(RuntimeError):
    """Discord refused a request.

    *status* is the HTTP status; *code* is Discord's own error code, which is
    what distinguishes "unknown channel" from "missing permissions" and is
    worth putting in front of the operator.
    """

    def __init__(self, status: int, message: str, code: int = 0) -> None:
        super().__init__(message)
        self.status = status
        self.code = code

    @property
    def is_auth_failure(self) -> bool:
        return self.status == 401

    @property
    def is_forbidden(self) -> bool:
        return self.status == 403

    def explain(self) -> str:
        """The failure in the operator's terms, where we can manage one."""
        if self.is_auth_failure:
            return (
                "Discord rejected the bot token. Check it in the Developer "
                "Portal under Bot, and use Reset Token if you no longer have it."
            )
        if self.is_forbidden:
            return (
                "The bot is not allowed to do that. Check that it has been "
                "invited to the server and can View Channel, Send Messages "
                "and Embed Links in the channel you chose."
            )
        if self.status == 404:
            return (
                "Discord does not know that id. Check the channel or server "
                "ID -- right-click it in Discord with Developer Mode on."
            )
        # Status 0 is ours, not Discord's: it means the request never got
        # there, and the message already says what happened.
        if not self.status:
            return str(self)
        return f"Discord returned {self.status}: {self}"


class RestClient:
    """A thin, rate-limit-aware wrapper over Discord's REST API.

    The token is held here and never logged: errors are reported by status
    and Discord's own code, and the Authorization header is not echoed.
    """

    def __init__(
        self,
        token: str,
        *,
        api_base: str = DEFAULT_API_BASE,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = token
        self.api_base = api_base.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
        #: When each route bucket is next free, from the reset headers.
        self._next_free: dict[str, float] = {}

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ #
    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bot {self._token}",
            # Discord asks bots to identify themselves, and a library-shaped
            # user agent is what their rate-limit tooling expects to see.
            "User-Agent": "vhsm (https://github.com/JoSolv/VH-servermanager, 0.1)",
            "Content-Type": "application/json",
        }

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        bucket: str = "",
    ) -> Any:
        """One API call, retried for rate limits and transient failures.

        *bucket* names the rate-limit scope for pacing. Discord's buckets are
        per-route-and-major-parameter, so "the messages route of channel X" is
        the useful granularity and the caller supplies it.
        """
        url = f"{self.api_base}{path}"
        bucket = bucket or f"{method}:{path}"
        last: DiscordAPIError | None = None

        for attempt in range(MAX_ATTEMPTS):
            await self._wait_for_bucket(bucket)
            try:
                response = await self._client.request(
                    method, url, headers=self._headers, json=json
                )
            except httpx.HTTPError as exc:
                # Network trouble, not refusal: worth one more try, since the
                # manager may well be starting before its network is up.
                last = DiscordAPIError(0, f"could not reach Discord: {exc}")
                if attempt == MAX_ATTEMPTS - 1:
                    break
                await asyncio.sleep(min(2 ** attempt, MAX_RETRY_SLEEP))
                continue

            self._remember_bucket(bucket, response)

            if response.status_code == 429:
                retry_after = _retry_after(response)
                if retry_after > MAX_RETRY_SLEEP or attempt == MAX_ATTEMPTS - 1:
                    raise DiscordAPIError(
                        429, f"rate limited, retry after {retry_after:.1f}s"
                    )
                log.debug("rate limited on %s, waiting %.2fs", bucket, retry_after)
                await asyncio.sleep(retry_after)
                continue

            if response.status_code >= 500:
                last = DiscordAPIError(response.status_code, "Discord is having trouble")
                if attempt == MAX_ATTEMPTS - 1:
                    break
                await asyncio.sleep(min(2 ** attempt, MAX_RETRY_SLEEP))
                continue

            if response.status_code >= 400:
                raise _error_from(response)

            if response.status_code == 204 or not response.content:
                return None
            try:
                return response.json()
            except ValueError:
                return None

        raise last or DiscordAPIError(0, "request failed")

    # -- rate-limit bookkeeping ----------------------------------------- #
    async def _wait_for_bucket(self, bucket: str) -> None:
        free_at = self._next_free.get(bucket, 0.0)
        delay = free_at - time.monotonic()
        if delay > 0:
            await asyncio.sleep(min(delay, MAX_RETRY_SLEEP))

    def _remember_bucket(self, bucket: str, response: httpx.Response) -> None:
        """Note when this bucket is next free, if the response said.

        Only acted on when the remaining count has run out: pacing every
        request to the reset window would serialise announcements that
        Discord was perfectly happy to take at once.
        """
        remaining = response.headers.get("X-RateLimit-Remaining")
        reset_after = response.headers.get("X-RateLimit-Reset-After")
        if remaining is None or reset_after is None:
            return
        try:
            if int(remaining) <= 0:
                self._next_free[bucket] = time.monotonic() + float(reset_after)
        except ValueError:
            pass

    # ------------------------------------------------------------------ #
    # the calls the bot actually makes
    # ------------------------------------------------------------------ #
    async def gateway_url(self) -> str:
        """The websocket URL to connect to, as Discord asks bots to ask."""
        payload = await self.request("GET", "/gateway/bot")
        url = str((payload or {}).get("url") or "")
        if not url:
            raise DiscordAPIError(0, "Discord did not return a gateway URL")
        return url

    async def me(self) -> dict[str, Any]:
        return await self.request("GET", "/users/@me") or {}

    async def channel(self, channel_id: str) -> dict[str, Any]:
        """One channel, which is how the guild behind it is discovered.

        The operator pastes a channel id and nothing else; the guild is needed
        to register commands, so it is looked up here rather than asked for.
        """
        return await self.request("GET", f"/channels/{channel_id}") or {}

    async def create_message(self, channel_id: str, payload: dict[str, Any]) -> Any:
        return await self.request(
            "POST", f"/channels/{channel_id}/messages",
            json=payload, bucket=f"messages:{channel_id}",
        )

    async def edit_message(
        self, channel_id: str, message_id: str, payload: dict[str, Any]
    ) -> Any:
        return await self.request(
            "PATCH", f"/channels/{channel_id}/messages/{message_id}",
            json=payload, bucket=f"messages:{channel_id}",
        )

    async def register_guild_commands(
        self, application_id: str, guild_id: str, commands: list[dict[str, Any]]
    ) -> Any:
        """Replace this guild's command set in one call.

        Guild-scoped rather than global: it takes effect immediately, where a
        global registration can take an hour to propagate, and this bot lives
        in exactly one guild by design.
        """
        return await self.request(
            "PUT", f"/applications/{application_id}/guilds/{guild_id}/commands",
            json=commands,
        )

    async def respond_to_interaction(
        self, interaction_id: str, token: str, payload: dict[str, Any]
    ) -> None:
        """Answer an interaction. Discord allows three seconds for this."""
        await self.request(
            "POST", f"/interactions/{interaction_id}/{token}/callback",
            json=payload, bucket="interaction-callback",
        )

    async def edit_interaction_response(
        self, application_id: str, token: str, payload: dict[str, Any]
    ) -> None:
        """Fill in a reply that was deferred."""
        await self.request(
            "PATCH", f"/webhooks/{application_id}/{token}/messages/@original",
            json=payload, bucket="interaction-followup",
        )


def _retry_after(response: httpx.Response) -> float:
    """How long a 429 wants us to wait, from the body or the header."""
    try:
        body = response.json()
        if isinstance(body, dict) and "retry_after" in body:
            return max(0.0, float(body["retry_after"]))
    except ValueError:
        pass
    try:
        return max(0.0, float(response.headers.get("Retry-After", "1")))
    except ValueError:
        return 1.0


def _error_from(response: httpx.Response) -> DiscordAPIError:
    message, code = response.reason_phrase or "refused", 0
    try:
        body = response.json()
        if isinstance(body, dict):
            message = str(body.get("message") or message)
            code = int(body.get("code") or 0)
    except ValueError:
        pass
    return DiscordAPIError(response.status_code, message, code)
