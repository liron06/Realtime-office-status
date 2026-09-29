class MinecraftService:
    """Future async boundary for Minecraft and RCON operations."""

    async def not_configured(self) -> str:
        return "Minecraft server integration has not been configured yet."
