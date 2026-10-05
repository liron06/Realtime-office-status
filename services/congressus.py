import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlencode

import aiohttp
import discord
from aiohttp import web

from config import Config
from services.database import (
    CongressusAccountAlreadyLinked,
    DatabaseService,
    DiscordAccountAlreadyLinked,
    StorageError,
)


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
    def __init__(
        self,
        bot: discord.Client,
        config: Config,
        database: DatabaseService,
    ) -> None:
        self.bot = bot
        self.config = config
        self.database = database
        self._states: dict[str, PendingAuthorization] = {}
        self._state_lock = asyncio.Lock()
        self._session: aiohttp.ClientSession | None = None
        self._runner: web.AppRunner | None = None
        self._verified_callback: Callable[[int], Awaitable[None]] | None = None

    def set_verified_callback(
        self, callback: Callable[[int], Awaitable[None]] | None
    ) -> None:
        self._verified_callback = callback

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
                "Verificatie mislukt",
                "Deze verificatielink ontbreekt, is ongeldig, verlopen of al gebruikt.",
                status=400,
            )

        assert pending is not None
        if request.query.get("error"):
            LOGGER.info(
                "Congressus authorization was declined for Discord user %d",
                pending.discord_user_id,
            )
            return self._html_response(
                "Verificatie geannuleerd",
                "Congressus heeft de verificatie niet goedgekeurd. Ga terug naar Discord om het opnieuw te proberen.",
                status=400,
            )

        code = request.query.get("code")
        if not code:
            LOGGER.warning(
                "Congressus callback contained no authorization code for Discord user %d",
                pending.discord_user_id,
            )
            return self._html_response(
                "Verificatie mislukt",
                "Congressus gaf geen geldige autorisatie terug.",
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
                "Verificatie mislukt",
                "Congressus kon de verificatie niet afronden. Probeer het later opnieuw via Discord.",
                status=502,
            )

        if userinfo.get("is_active") is not True:
            LOGGER.info(
                "Congressus membership is not active for Discord user %d",
                pending.discord_user_id,
            )
            return self._html_response(
                "Lidmaatschap niet actief",
                "Voor Minecraft-toegang is een actief Realtime-lidmaatschap nodig.",
                status=403,
            )

        congressus_user_id = _required_identifier(userinfo.get("user_id"))
        if congressus_user_id is None:
            LOGGER.error(
                "Congressus userinfo did not contain a usable user ID for Discord user %d",
                pending.discord_user_id,
            )
            return self._html_response(
                "Verificatie mislukt",
                "Congressus gaf onvolledige lidmaatschapsgegevens terug. Probeer het later opnieuw.",
                status=502,
            )

        try:
            await self.database.link_member(
                pending.discord_user_id,
                congressus_user_id,
                _optional_profile_value(userinfo.get("username")),
                _optional_profile_value(userinfo.get("name")),
            )
        except DiscordAccountAlreadyLinked:
            LOGGER.warning(
                "Rejected a different Congressus account for Discord user %d",
                pending.discord_user_id,
            )
            return self._html_response(
                "Account al gekoppeld",
                "Dit Discord-account is al gekoppeld aan een ander Congressus-account. Neem contact op met een beheerder als dit niet klopt.",
                status=409,
            )
        except CongressusAccountAlreadyLinked:
            LOGGER.warning(
                "Rejected an already-linked Congressus account for Discord user %d",
                pending.discord_user_id,
            )
            return self._html_response(
                "Account al gekoppeld",
                "Dit Congressus-account is al gekoppeld aan een ander Discord-account. Neem contact op met een beheerder als dit niet klopt.",
                status=409,
            )
        except StorageError as error:
            LOGGER.error(
                "Could not persist Congressus validation for Discord user %d (%s)",
                pending.discord_user_id,
                type(error).__name__,
            )
            return self._html_response(
                "Verificatie mislukt",
                "Je lidmaatschap is geverifieerd, maar de accountkoppeling kon niet worden opgeslagen. Probeer het later opnieuw.",
                status=500,
            )

        if not await self._assign_validated_role(pending.discord_user_id):
            return self._html_response(
                "Verificatie mislukt",
                "Je lidmaatschap is geverifieerd, maar de Discord-rol kon niet worden toegewezen. Neem contact op met een beheerder.",
                status=500,
            )

        if self._verified_callback is not None:
            try:
                await self._verified_callback(pending.discord_user_id)
            except Exception as error:
                LOGGER.error(
                    "Could not send Minecraft onboarding prompt to Discord user %d (%s)",
                    pending.discord_user_id,
                    type(error).__name__,
                )

        LOGGER.info(
            "Congressus membership validated for Discord user %d", pending.discord_user_id
        )
        return self._html_response(
            "Verificatie geslaagd",
            "Je Realtime-lidmaatschap is geverifieerd. Ga terug naar Discord om je Minecraft-account te koppelen.",
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
<html lang="nl">
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


def _required_identifier(value: object) -> str | None:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    identifier = str(value).strip()
    return identifier if identifier else None


def _optional_profile_value(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned[:255] if cleaned else None
