import asyncio
import logging
import re
from typing import Any

import aiohttp

from config import Config
from services.database import DatabaseService, MemberLink, MemberNotValidated, StorageError


MINECRAFT_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{3,16}$")
LOGGER = logging.getLogger(__name__)
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=8, connect=3, sock_read=5)


def is_valid_minecraft_username(username: str) -> bool:
    return MINECRAFT_USERNAME_PATTERN.fullmatch(username) is not None


class MinecraftApiError(Exception):
    """Safe base error for Minecraft API failures."""


class MinecraftApiUnavailable(MinecraftApiError):
    pass


class MinecraftApiTimeout(MinecraftApiError):
    pass


class MinecraftApiUnauthorized(MinecraftApiError):
    pass


class MinecraftOperationFailed(MinecraftApiError):
    pass


class MinecraftMalformedResponse(MinecraftApiError):
    pass


class RegistrationSyncFailed(MinecraftApiError):
    """The desired username was stored, but the API sync failed."""

    def __init__(self, cause: MinecraftApiError) -> None:
        self.cause = cause
        super().__init__("Minecraft whitelist synchronization failed after registration")


class WhitelistChangeFailed(MinecraftApiError):
    def __init__(self, cause: MinecraftApiError, *, rollback_failed: bool = False) -> None:
        self.cause = cause
        self.rollback_failed = rollback_failed
        super().__init__("Minecraft whitelist change failed")


class MinecraftApiClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=HTTP_TIMEOUT)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def health(self) -> dict[str, Any] | list[Any]:
        return await self._request_json("GET", "/health")

    async def status(self) -> dict[str, Any] | list[Any]:
        return await self._request_json("GET", "/status")

    async def players(self) -> dict[str, Any] | list[Any]:
        return await self._request_json("GET", "/players")

    async def whitelist_add(self, username: str) -> None:
        await self._request("POST", "/whitelist/add", json={"username": username})

    async def whitelist_remove(self, username: str) -> None:
        await self._request("POST", "/whitelist/remove", json={"username": username})

    async def server_start(self) -> None:
        await self._request("POST", "/server/start")

    async def server_stop(self) -> None:
        await self._request("POST", "/server/stop")

    async def server_restart(self) -> None:
        await self._request("POST", "/server/restart")

    async def _request_json(self, method: str, path: str) -> dict[str, Any] | list[Any]:
        response = await self._request(method, path, parse_json=True)
        if not isinstance(response, (dict, list)):
            LOGGER.warning("Minecraft API returned an unexpected JSON type for %s", path)
            raise MinecraftMalformedResponse("Minecraft API returned an invalid response")
        return response

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, str] | None = None,
        parse_json: bool = False,
    ) -> object | None:
        session = self._require_session()
        try:
            async with session.request(
                method,
                f"{self.base_url}{path}",
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/json",
                },
                json=json,
            ) as response:
                if response.status in {401, 403}:
                    LOGGER.error("Minecraft API rejected authentication for %s", path)
                    raise MinecraftApiUnauthorized("Minecraft API authentication failed")
                if response.status < 200 or response.status >= 300:
                    LOGGER.warning(
                        "Minecraft API operation %s returned HTTP %d", path, response.status
                    )
                    raise MinecraftOperationFailed("Minecraft API operation failed")
                if not parse_json:
                    return None
                try:
                    return await response.json(content_type=None)
                except (aiohttp.ClientError, ValueError):
                    LOGGER.warning("Minecraft API returned malformed JSON for %s", path)
                    raise MinecraftMalformedResponse(
                        "Minecraft API returned an invalid response"
                    ) from None
        except MinecraftApiError:
            raise
        except asyncio.TimeoutError:
            LOGGER.warning("Minecraft API request timed out for %s", path)
            raise MinecraftApiTimeout("Minecraft API request timed out") from None
        except aiohttp.ClientError as error:
            LOGGER.warning(
                "Minecraft API is unreachable for %s (%s)", path, type(error).__name__
            )
            raise MinecraftApiUnavailable("Minecraft API is unavailable") from None

    def _require_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            raise MinecraftApiUnavailable("Minecraft API client is not running")
        return self._session


class MinecraftService:
    def __init__(
        self,
        config: Config,
        database: DatabaseService,
        api: MinecraftApiClient | None = None,
    ) -> None:
        self.database = database
        self.api = api or MinecraftApiClient(
            config.minecraft_api_url,
            config.minecraft_api_token,
        )
        self._account_lock = asyncio.Lock()

    async def start(self) -> None:
        await self.api.start()

    async def close(self) -> None:
        await self.api.close()

    async def register(self, discord_user_id: int, username: str) -> MemberLink:
        async with self._account_lock:
            member = await self.database.register_minecraft_username(
                discord_user_id, username
            )
            try:
                await self.api.whitelist_add(username)
            except MinecraftApiError as error:
                raise RegistrationSyncFailed(error) from None
            return member

    async def reset(self, discord_user_id: int) -> str | None:
        async with self._account_lock:
            member = await self.database.get_member(discord_user_id)
            if member is None:
                raise MemberNotValidated
            username = member.minecraft_username
            if username is None:
                return None

            await self.api.whitelist_remove(username)
            try:
                await self.database.reset_minecraft_username(discord_user_id)
            except StorageError:
                await self._restore_whitelist(username)
                raise
            return username

    async def set_username(self, discord_user_id: int, username: str) -> MemberLink:
        async with self._account_lock:
            member = await self.database.get_member(discord_user_id)
            if member is None:
                raise MemberNotValidated
            await self.database.ensure_minecraft_username_available(discord_user_id, username)
            old_username = member.minecraft_username
            replacing = (
                old_username is not None and old_username.casefold() != username.casefold()
            )

            if replacing:
                await self.api.whitelist_remove(old_username)

            try:
                await self.api.whitelist_add(username)
            except MinecraftApiError as error:
                rollback_failed = False
                if replacing:
                    rollback_failed = not await self._restore_whitelist(old_username)
                raise WhitelistChangeFailed(
                    error, rollback_failed=rollback_failed
                ) from None

            try:
                return await self.database.register_minecraft_username(
                    discord_user_id, username, replace=True
                )
            except StorageError:
                await self._rollback_database_failure(old_username, username, replacing)
                raise

    async def sync(self, discord_user_id: int) -> str | None:
        async with self._account_lock:
            member = await self.database.get_member(discord_user_id)
            if member is None:
                raise MemberNotValidated
            if member.minecraft_username is None:
                return None
            await self.api.whitelist_add(member.minecraft_username)
            return member.minecraft_username

    async def _restore_whitelist(self, username: str) -> bool:
        try:
            await self.api.whitelist_add(username)
            return True
        except MinecraftApiError as error:
            LOGGER.error(
                "Could not restore a previous Minecraft whitelist entry (%s)",
                type(error).__name__,
            )
            return False

    async def _rollback_database_failure(
        self,
        old_username: str | None,
        new_username: str,
        replacing: bool,
    ) -> None:
        try:
            if old_username is None:
                await self.api.whitelist_remove(new_username)
            elif replacing:
                await self.api.whitelist_remove(new_username)
                await self.api.whitelist_add(old_username)
        except MinecraftApiError as error:
            LOGGER.error(
                "Could not roll back Minecraft whitelist after a database failure (%s)",
                type(error).__name__,
            )
