import os
from dataclasses import dataclass

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
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    try:
        parsed = int(value)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be a Discord ID") from error
    if parsed <= 0:
        raise ConfigurationError(f"{name} must be a positive Discord ID")
    return parsed


@dataclass(frozen=True, slots=True)
class Config:
    discord_token: str
    office_voice_channel_id: int
    office_control_channel_id: int
    office_manager_role_id: int
    minecraft_manager_role_id: int
    discord_guild_id: int | None = None
    minecraft_role_id: int | None = None

    @classmethod
    def from_environment(cls) -> "Config":
        load_dotenv()
        return cls(
            discord_token=_required("DISCORD_TOKEN"),
            discord_guild_id=_optional_id("DISCORD_GUILD_ID"),
            office_voice_channel_id=_required_id("OFFICE_VOICE_CHANNEL_ID"),
            office_control_channel_id=_required_id("OFFICE_CONTROL_CHANNEL_ID"),
            office_manager_role_id=_required_id("OFFICE_MANAGER_ROLE_ID"),
            minecraft_manager_role_id=_required_id("MINECRAFT_MANAGER_ROLE_ID"),
            minecraft_role_id=_optional_id("MINECRAFT_ROLE_ID"),
        )
