import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv


class ConfigurationError(ValueError):
    """Raised when required environment configuration is missing or invalid."""


def _required(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ConfigurationError(f"{name} must be set")
    return value.strip()


def _required_id(name: str) -> int:
    value = _required(name)
    try:
        parsed = int(value)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be a Discord ID") from error
    if parsed <= 0:
        raise ConfigurationError(f"{name} must be a positive Discord ID")
    return parsed


def _optional_id(name: str) -> int | None:
    value = (os.getenv(name) or "").strip()
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be a Discord ID") from error
    if parsed <= 0:
        raise ConfigurationError(f"{name} must be a positive Discord ID")
    return parsed


def _required_https_url(name: str) -> str:
    value = _required(name)
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError(
            f"{name} must be an HTTPS URL without credentials, a query, or a fragment"
        )
    return value


def _required_api_url(name: str) -> str:
    value = _required(name)
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError(
            f"{name} must be an HTTP(S) URL without credentials, a query, or a fragment"
        )
    return value.rstrip("/")


def _database_path() -> Path:
    configured = (os.getenv("DATABASE_PATH") or "").strip()
    return Path(configured or "data/realtime.db").expanduser()


@dataclass(frozen=True, slots=True)
class Config:
    discord_token: str
    office_voice_channel_id: int
    office_control_channel_id: int
    office_manager_role_id: int
    minecraft_manager_role_id: int
    board_role_id: int
    discord_guild_id: int
    minecraft_role_id: int
    congressus_client_id: str
    congressus_client_secret: str
    congressus_base_url: str
    congressus_redirect_uri: str
    minecraft_api_url: str
    minecraft_api_token: str
    minecraft_server_host: str
    database_path: Path = Path("data/realtime.db")
    reclametime_channel_id: int | None = None
    careertime_channel_id: int | None = None
    commission_channel_id: int | None = None
    minecraft_channel_id: int | None = None

    def __post_init__(self) -> None:
        if self.minecraft_role_id in {
            self.office_manager_role_id,
            self.minecraft_manager_role_id,
            self.board_role_id,
        }:
            raise ConfigurationError(
                "MINECRAFT_ROLE_ID must be different from all management role IDs"
            )

    @classmethod
    def from_environment(cls) -> "Config":
        load_dotenv()
        return cls(
            discord_token=_required("DISCORD_TOKEN"),
            discord_guild_id=_required_id("DISCORD_GUILD_ID"),
            office_voice_channel_id=_required_id("OFFICE_VOICE_CHANNEL_ID"),
            office_control_channel_id=_required_id("OFFICE_CONTROL_CHANNEL_ID"),
            office_manager_role_id=_required_id("OFFICE_MANAGER_ROLE_ID"),
            minecraft_manager_role_id=_required_id("MINECRAFT_MANAGER_ROLE_ID"),
            board_role_id=_required_id("BOARD_ROLE_ID"),
            minecraft_role_id=_required_id("MINECRAFT_ROLE_ID"),
            congressus_client_id=_required("CONGRESSUS_CLIENT_ID"),
            congressus_client_secret=_required("CONGRESSUS_CLIENT_SECRET"),
            congressus_base_url=_required_https_url("CONGRESSUS_BASE_URL").rstrip("/"),
            congressus_redirect_uri=_required_https_url("CONGRESSUS_REDIRECT_URI"),
            minecraft_api_url=_required_api_url("MINECRAFT_API_URL"),
            minecraft_api_token=_required("MINECRAFT_API_TOKEN"),
            minecraft_server_host=_required("MINECRAFT_SERVER_HOST"),
            database_path=_database_path(),
            reclametime_channel_id=_optional_id("RECLAMETIME_CHANNEL_ID"),
            careertime_channel_id=_optional_id("CAREERTIME_CHANNEL_ID"),
            commission_channel_id=_optional_id("COMMISSION_CHANNEL_ID"),
            minecraft_channel_id=_optional_id("MINECRAFT_CHANNEL_ID"),
        )
