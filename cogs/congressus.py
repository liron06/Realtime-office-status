from discord.ext import commands

from services.congressus import CongressusService


class CongressusCog(commands.Cog):
    """Owns the Congressus OAuth service lifecycle."""

    def __init__(self, bot: commands.Bot) -> None:
        self.service = CongressusService(bot, bot.config)

    async def cog_load(self) -> None:
        await self.service.start()

    async def cog_unload(self) -> None:
        await self.service.stop()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CongressusCog(bot))
