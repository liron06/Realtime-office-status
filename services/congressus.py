import asyncio
import logging
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import aiohttp
import discord
from aiohttp import web

from config import Config


LOGGER = logging.getLogger(__name__)
CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 8090
CALLBACK_PATH = "/congressus/callback"
STATE_LIFETIME_SECONDS = 10 * 60
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10, connect=5)


@dataclass(frozen=True, slots=True)
class PendingAuthorization:
    discord_user_id: int
    expires_at: float


class CongressusUpstreamError(Exception):
    """Raised when Congressus cannot complete a validation request."""


class CongressusService:
    def __init__(self, bot: discord.Client, config: Config) -> None:
        self.bot = bot
        self.config = config
        self._states: dict[str, PendingAuthorization] = {}
        self._state_lock = asyncio.Lock()
        self._session: aiohttp.ClientSession | None = None
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        if self._runner is not None:
            return

        self._session = aiohttp.ClientSession(timeout=HTTP_TIMEOUT)
        application = web.Application(client_max_size=1024)
        application.router.add_get(CALLBACK_PATH, self._handle_callback)
        self._runner = web.AppRunner(application, access_log=None)

        try:
            await self._runner.setup()
            site = web.TCPSite(self._runner, CALLBACK_HOST, CALLBACK_PORT)
            await site.start()
        except Exception:
            await self.stop()
            raise

        LOGGER.info(
            "Congressus OAuth callback server listening on http://%s:%d%s",
            CALLBACK_HOST,
            CALLBACK_PORT,
            CALLBACK_PATH,
        )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self._session is not None:
            await self._session.close()
            self._session = None
        async with self._state_lock:
            self._states.clear()
        LOGGER.info("Congressus OAuth service stopped")

    async def create_authorization_url(self, discord_user_id: int) -> str:
        state = secrets.token_urlsafe(32)
        now = time.monotonic()
        pending = PendingAuthorization(
            discord_user_id=discord_user_id,
            expires_at=now + STATE_LIFETIME_SECONDS,
        )

        async with self._state_lock:
            self._states = {
                key: value
                for key, value in self._states.items()
                if value.expires_at > now and value.discord_user_id != discord_user_id
            }
            self._states[state] = pending

        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.config.congressus_client_id,
                "redirect_uri": self.config.congressus_redirect_uri,
                "scope": "openid profile",
                "state": state,
            }
        )
        return f"{self.config.congressus_base_url}/oauth/authorize?{query}"

    async def _consume_state(
        self, state: str | None
    ) -> tuple[PendingAuthorization | None, str | None]:
        if not state:
            return None, "missing"

        async with self._state_lock:
            pending = self._states.pop(state, None)

        if pending is None:
            return None, "invalid_or_used"
        if pending.expires_at <= time.monotonic():
            return None, "expired"
        return pending, None

    async def _handle_callback(self, request: web.Request) -> web.Response:
        pending, state_error = await self._consume_state(request.query.get("state"))
        if state_error is not None:
            LOGGER.warning("Rejected Congressus callback with %s state", state_error)
            return self._html_response(
                "Validation failed",
                "This validation link is missing, invalid, expired, or has already been used.",
                status=400,
            )

        assert pending is not None
        if request.query.get("error"):
            LOGGER.info(
                "Congressus authorization was declined for Discord user %d",
                pending.discord_user_id,
            )
            return self._html_response(
                "Validation cancelled",
                "Congressus did not approve the validation request. You can return to Discord and try again.",
                status=400,
            )

        code = request.query.get("code")
        if not code:
            LOGGER.warning(
                "Congressus callback contained no authorization code for Discord user %d",
                pending.discord_user_id,
            )
            return self._html_response(
                "Validation failed",
                "Congressus did not return an authorization code.",
                status=400,
            )

        try:
            access_token = await self._exchange_code(code)
            userinfo = await self._fetch_userinfo(access_token)
        except CongressusUpstreamError as error:
            LOGGER.error(
                "Congressus validation request failed for Discord user %d: %s",
                pending.discord_user_id,
                error,
            )
            return self._html_response(
                "Validation failed",
                "Congressus could not complete the validation. Please return to Discord and try again later.",
                status=502,
            )

        if userinfo.get("is_active") is not True:
            LOGGER.info(
                "Congressus membership is not active for Discord user %d",
                pending.discord_user_id,
            )
            return self._html_response(
                "Membership not active",
                "An active Realtime membership is required for Minecraft access.",
                status=403,
            )

        if not await self._assign_validated_role(pending.discord_user_id):
            return self._html_response(
                "Validation failed",
                "Your membership was verified, but the Discord role could not be assigned. Please contact an administrator.",
                status=500,
            )

        LOGGER.info(
            "Congressus membership validated for Discord user %d", pending.discord_user_id
        )
        return self._html_response(
            "Validation successful",
            "Your active Realtime membership was verified and your Discord role was assigned. You can close this page.",
        )

    async def _exchange_code(self, code: str) -> str:
        session = self._require_session()
        try:
            async with session.post(
                f"{self.config.congressus_base_url}/oauth/token",
                auth=aiohttp.BasicAuth(
                    self.config.congressus_client_id,
                    self.config.congressus_client_secret,
                ),
                data={"grant_type": "authorization_code", "code": code},
                headers={"Accept": "application/json"},
            ) as response:
                if response.status != 200:
                    LOGGER.warning("Congressus token endpoint returned HTTP %d", response.status)
                    raise CongressusUpstreamError("Token exchange was rejected")
                payload = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
            raise CongressusUpstreamError(
                f"Token request failed ({type(error).__name__})"
            ) from None

        if not isinstance(payload, dict):
            raise CongressusUpstreamError("Token endpoint returned an invalid response")
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise CongressusUpstreamError("Token response did not contain an access token")
        return access_token

    async def _fetch_userinfo(self, access_token: str) -> dict[str, object]:
        session = self._require_session()
        try:
            async with session.get(
                f"{self.config.congressus_base_url}/oauth/userinfo",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
            ) as response:
                if response.status != 200:
                    LOGGER.warning("Congressus userinfo endpoint returned HTTP %d", response.status)
                    raise CongressusUpstreamError("User information request was rejected")
                payload = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
            raise CongressusUpstreamError(
                f"User information request failed ({type(error).__name__})"
            ) from None

        if not isinstance(payload, dict):
            raise CongressusUpstreamError("Userinfo endpoint returned an invalid response")
        return payload

    async def _assign_validated_role(self, discord_user_id: int) -> bool:
        guild = self.bot.get_guild(self.config.discord_guild_id)
        if guild is None:
            LOGGER.error("Configured Discord guild is not available")
            return False

        role = guild.get_role(self.config.minecraft_role_id)
        if role is None:
            LOGGER.error("Configured validated Minecraft role is not available")
            return False

        member = guild.get_member(discord_user_id)
        if member is None:
            try:
                member = await guild.fetch_member(discord_user_id)
            except discord.HTTPException as error:
                LOGGER.error(
                    "Could not retrieve Discord member %d (%s)",
                    discord_user_id,
                    type(error).__name__,
                )
                return False

        if role in member.roles:
            return True

        try:
            await member.add_roles(role, reason="Active Congressus membership verified")
        except discord.HTTPException as error:
            LOGGER.error(
                "Could not assign validated Minecraft role to Discord user %d (%s)",
                discord_user_id,
                type(error).__name__,
            )
            return False
        return True

    def _require_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            raise CongressusUpstreamError("OAuth service is not running")
        return self._session

    @staticmethod
    def _html_response(title: str, message: str, status: int = 200) -> web.Response:
        html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 42rem; margin: 5rem auto; padding: 0 1.5rem; color: #202124; }}
    main {{ border: 1px solid #ddd; border-radius: 12px; padding: 2rem; }}
  </style>
</head>
<body><main><h1>{title}</h1><p>{message}</p></main></body>
</html>"""
        return web.Response(
            text=html,
            status=status,
            content_type="text/html",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )
