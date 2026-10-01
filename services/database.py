import asyncio
import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


LOGGER = logging.getLogger(__name__)


class StorageError(Exception):
    """Raised when member-link storage cannot complete an operation."""


class DiscordAccountAlreadyLinked(StorageError):
    pass


class CongressusAccountAlreadyLinked(StorageError):
    pass


class MemberNotValidated(StorageError):
    pass


class MinecraftUsernameAlreadySet(StorageError):
    def __init__(self, username: str) -> None:
        self.username = username
        super().__init__("A Minecraft username is already registered")


class MinecraftUsernameClaimed(StorageError):
    pass


@dataclass(frozen=True, slots=True)
class MemberLink:
    discord_user_id: int
    congressus_user_id: str
    congressus_username: str | None
    congressus_name: str | None
    minecraft_username: str | None
    verified_at: str
    minecraft_registered_at: str | None


class DatabaseService:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._write_lock = asyncio.Lock()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)
        LOGGER.info("SQLite member-link database initialized at %s", self.path)

    async def link_member(
        self,
        discord_user_id: int,
        congressus_user_id: str,
        congressus_username: str | None,
        congressus_name: str | None,
    ) -> MemberLink:
        async with self._write_lock:
            return await asyncio.to_thread(
                self._link_member_sync,
                discord_user_id,
                congressus_user_id,
                congressus_username,
                congressus_name,
            )

    async def get_member(self, discord_user_id: int) -> MemberLink | None:
        return await asyncio.to_thread(self._get_member_sync, discord_user_id)

    async def register_minecraft_username(
        self,
        discord_user_id: int,
        username: str,
        *,
        replace: bool = False,
    ) -> MemberLink:
        async with self._write_lock:
            return await asyncio.to_thread(
                self._register_minecraft_username_sync,
                discord_user_id,
                username,
                replace,
            )

    async def reset_minecraft_username(self, discord_user_id: int) -> bool:
        async with self._write_lock:
            return await asyncio.to_thread(self._reset_minecraft_username_sync, discord_user_id)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize_sync(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS minecraft_members (
                        discord_user_id INTEGER PRIMARY KEY,
                        congressus_user_id TEXT NOT NULL UNIQUE,
                        congressus_username TEXT,
                        congressus_name TEXT,
                        minecraft_username TEXT COLLATE NOCASE UNIQUE,
                        verified_at TEXT NOT NULL,
                        minecraft_registered_at TEXT
                    )
                    """
                )
                connection.commit()
        except (OSError, sqlite3.Error) as error:
            raise StorageError("Could not initialize the member-link database") from error

    def _link_member_sync(
        self,
        discord_user_id: int,
        congressus_user_id: str,
        congressus_username: str | None,
        congressus_name: str | None,
    ) -> MemberLink:
        verified_at = _utc_now()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                discord_link = connection.execute(
                    "SELECT congressus_user_id FROM minecraft_members WHERE discord_user_id = ?",
                    (discord_user_id,),
                ).fetchone()
                if (
                    discord_link is not None
                    and discord_link["congressus_user_id"] != congressus_user_id
                ):
                    raise DiscordAccountAlreadyLinked

                congressus_link = connection.execute(
                    "SELECT discord_user_id FROM minecraft_members WHERE congressus_user_id = ?",
                    (congressus_user_id,),
                ).fetchone()
                if (
                    congressus_link is not None
                    and congressus_link["discord_user_id"] != discord_user_id
                ):
                    raise CongressusAccountAlreadyLinked

                if discord_link is None:
                    connection.execute(
                        """
                        INSERT INTO minecraft_members (
                            discord_user_id,
                            congressus_user_id,
                            congressus_username,
                            congressus_name,
                            verified_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            discord_user_id,
                            congressus_user_id,
                            congressus_username,
                            congressus_name,
                            verified_at,
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE minecraft_members
                        SET congressus_username = ?, congressus_name = ?, verified_at = ?
                        WHERE discord_user_id = ?
                        """,
                        (congressus_username, congressus_name, verified_at, discord_user_id),
                    )
                connection.commit()
        except (DiscordAccountAlreadyLinked, CongressusAccountAlreadyLinked):
            raise
        except sqlite3.Error as error:
            raise StorageError("Could not persist the Congressus validation") from error

        member = self._get_member_sync(discord_user_id)
        if member is None:
            raise StorageError("Persisted member link could not be read")
        return member

    def _get_member_sync(self, discord_user_id: int) -> MemberLink | None:
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT * FROM minecraft_members WHERE discord_user_id = ?",
                    (discord_user_id,),
                ).fetchone()
        except sqlite3.Error as error:
            raise StorageError("Could not read the member-link database") from error
        return _member_from_row(row) if row is not None else None

    def _register_minecraft_username_sync(
        self,
        discord_user_id: int,
        username: str,
        replace: bool,
    ) -> MemberLink:
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                member = connection.execute(
                    "SELECT minecraft_username FROM minecraft_members WHERE discord_user_id = ?",
                    (discord_user_id,),
                ).fetchone()
                if member is None:
                    raise MemberNotValidated
                if member["minecraft_username"] is not None and not replace:
                    raise MinecraftUsernameAlreadySet(member["minecraft_username"])

                claimed = connection.execute(
                    """
                    SELECT discord_user_id FROM minecraft_members
                    WHERE minecraft_username = ? COLLATE NOCASE AND discord_user_id != ?
                    """,
                    (username, discord_user_id),
                ).fetchone()
                if claimed is not None:
                    raise MinecraftUsernameClaimed

                connection.execute(
                    """
                    UPDATE minecraft_members
                    SET minecraft_username = ?, minecraft_registered_at = ?
                    WHERE discord_user_id = ?
                    """,
                    (username, _utc_now(), discord_user_id),
                )
                connection.commit()
        except (
            MemberNotValidated,
            MinecraftUsernameAlreadySet,
            MinecraftUsernameClaimed,
        ):
            raise
        except sqlite3.Error as error:
            raise StorageError("Could not store the Minecraft username") from error

        member_link = self._get_member_sync(discord_user_id)
        if member_link is None:
            raise StorageError("Updated member link could not be read")
        return member_link

    def _reset_minecraft_username_sync(self, discord_user_id: int) -> bool:
        try:
            with closing(self._connect()) as connection:
                existing = connection.execute(
                    "SELECT minecraft_username FROM minecraft_members WHERE discord_user_id = ?",
                    (discord_user_id,),
                ).fetchone()
                if existing is None:
                    raise MemberNotValidated
                connection.execute(
                    """
                    UPDATE minecraft_members
                    SET minecraft_username = NULL, minecraft_registered_at = NULL
                    WHERE discord_user_id = ?
                    """,
                    (discord_user_id,),
                )
                connection.commit()
                return existing["minecraft_username"] is not None
        except MemberNotValidated:
            raise
        except sqlite3.Error as error:
            raise StorageError("Could not reset the Minecraft username") from error


def _member_from_row(row: sqlite3.Row) -> MemberLink:
    return MemberLink(
        discord_user_id=row["discord_user_id"],
        congressus_user_id=row["congressus_user_id"],
        congressus_username=row["congressus_username"],
        congressus_name=row["congressus_name"],
        minecraft_username=row["minecraft_username"],
        verified_at=row["verified_at"],
        minecraft_registered_at=row["minecraft_registered_at"],
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
