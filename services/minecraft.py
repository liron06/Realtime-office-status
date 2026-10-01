import re


MINECRAFT_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{3,16}$")


def is_valid_minecraft_username(username: str) -> bool:
    return MINECRAFT_USERNAME_PATTERN.fullmatch(username) is not None


class MinecraftService:
    """Future async boundary for Minecraft and RCON operations."""

    async def not_configured(self) -> str:
        return "Minecraft server integration has not been configured yet."
