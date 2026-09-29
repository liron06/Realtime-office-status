import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from config import Config, ConfigurationError
from utils.permissions import ManagementPermissionError


LOGGER = logging.getLogger(__name__)
EXTENSIONS = ("cogs.office", "cogs.minecraft", "cogs.congressus")


class RealtimeBot(commands.Bot):
    def __init__(self, config: Config) -> None:
        super().__init__(command_prefix="!", intents=discord.Intents.default())
        self.config = config

    async def setup_hook(self) -> None:
        for extension in EXTENSIONS:
            await self.load_extension(extension)
            LOGGER.info("Loaded extension %s", extension)

        if self.config.discord_guild_id is not None:
            guild = discord.Object(id=self.config.discord_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            LOGGER.info("Synced %d application commands to the configured guild", len(synced))
        else:
            synced = await self.tree.sync()
            LOGGER.info("Synced %d global application commands", len(synced))


async def send_interaction_error(interaction: discord.Interaction, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def create_bot(config: Config) -> RealtimeBot:
    bot = RealtimeBot(config)

    @bot.event
    async def on_ready() -> None:
        LOGGER.info("Bot is online as %s", bot.user)

    @bot.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        original = getattr(error, "original", error)
        if isinstance(original, ManagementPermissionError):
            await send_interaction_error(interaction, str(original))
            return
        if isinstance(error, (app_commands.NoPrivateMessage, app_commands.CheckFailure)):
            await send_interaction_error(
                interaction,
                "This command can only be used by server members with the required permission.",
            )
            return

        LOGGER.error(
            "Unhandled application command error for /%s",
            interaction.command.qualified_name if interaction.command else "unknown",
            exc_info=(type(error), error, error.__traceback__),
        )
        await send_interaction_error(
            interaction,
            "Something went wrong while handling that command. Please try again later.",
        )

    return bot


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = Config.from_environment()
    except ConfigurationError as error:
        LOGGER.error("Invalid configuration: %s", error)
        raise SystemExit(2) from error

    bot = create_bot(config)
    async with bot:
        await bot.start(config.discord_token)


if __name__ == "__main__":
    asyncio.run(main())
