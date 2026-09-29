import discord
from discord import app_commands
from discord.ext import commands

from config import Config
from services.minecraft import MinecraftService
from utils.permissions import minecraft_manager_only


NOT_CONFIGURED = "Minecraft server integration has not been configured yet."


class MinecraftCog(commands.GroupCog, group_name="minecraft", group_description="Minecraft tools"):
    def __init__(self, bot: commands.Bot) -> None:
        self.config: Config = bot.config
        self.service = MinecraftService()

    async def _member_placeholder(self, interaction: discord.Interaction, message: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used by server members.", ephemeral=True
            )
            return
        await interaction.response.send_message(message, ephemeral=True)

    async def _management_placeholder(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(await self.service.not_configured(), ephemeral=True)

    @app_commands.command(name="validate", description="Validate your Realtime membership for Minecraft")
    async def validate(self, interaction: discord.Interaction) -> None:
        await self._member_placeholder(
            interaction, "Congressus verification has not been configured yet."
        )

    @app_commands.command(name="status", description="Show the Minecraft server status")
    async def status(self, interaction: discord.Interaction) -> None:
        await self._member_placeholder(interaction, NOT_CONFIGURED)

    @app_commands.command(name="players", description="Show the online Minecraft players")
    async def players(self, interaction: discord.Interaction) -> None:
        await self._member_placeholder(interaction, NOT_CONFIGURED)

    @app_commands.command(name="start", description="Start the Minecraft server")
    @minecraft_manager_only()
    async def start(self, interaction: discord.Interaction) -> None:
        await self._management_placeholder(interaction)

    @app_commands.command(name="stop", description="Stop the Minecraft server")
    @minecraft_manager_only()
    async def stop(self, interaction: discord.Interaction) -> None:
        await self._management_placeholder(interaction)

    @app_commands.command(name="restart", description="Restart the Minecraft server")
    @minecraft_manager_only()
    async def restart(self, interaction: discord.Interaction) -> None:
        await self._management_placeholder(interaction)

    @app_commands.command(name="whitelist", description="Manage the Minecraft whitelist")
    @minecraft_manager_only()
    async def whitelist(self, interaction: discord.Interaction) -> None:
        await self._management_placeholder(interaction)

    @app_commands.command(name="commands", description="Show available Minecraft management commands")
    @minecraft_manager_only()
    async def commands_list(self, interaction: discord.Interaction) -> None:
        await self._management_placeholder(interaction)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MinecraftCog(bot))
