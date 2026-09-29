from discord.ext import commands

from services.congressus import CongressusService


class CongressusCog(commands.Cog):
    """Owns future Discord-facing Congressus functionality."""

    def __init__(self) -> None:
        self.service = CongressusService()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CongressusCog())
