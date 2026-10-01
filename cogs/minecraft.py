import logging

import discord
from discord import app_commands
from discord.ext import commands

from cogs.congressus import CongressusCog
from config import Config
from services.database import (
    DatabaseService,
    MemberNotValidated,
    MinecraftUsernameAlreadySet,
    MinecraftUsernameClaimed,
    StorageError,
)
from services.minecraft import MinecraftService, is_valid_minecraft_username
from utils.permissions import has_role, minecraft_manager_only


NOT_CONFIGURED = "Minecraft server integration has not been configured yet."
LOGGER = logging.getLogger(__name__)


class MinecraftCog(commands.GroupCog, group_name="minecraft", group_description="Minecraft tools"):
    whitelist = app_commands.Group(
        name="whitelist",
        description="Manage validated Minecraft member accounts",
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.config: Config = bot.config
        self.database: DatabaseService = bot.database
        self.service = MinecraftService()
        congressus_cog = bot.get_cog("CongressusCog")
        if not isinstance(congressus_cog, CongressusCog):
            raise RuntimeError("CongressusCog must be loaded before MinecraftCog")
        self.congressus = congressus_cog.service

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
        if interaction.guild_id != self.config.discord_guild_id:
            await interaction.response.send_message(
                "This command can only be used in the Realtime Discord server.", ephemeral=True
            )
            return

        authorization_url = await self.congressus.create_authorization_url(interaction.user.id)
        view = discord.ui.View()
        view.add_item(
            discord.ui.Button(
                label="Validate with Congressus",
                style=discord.ButtonStyle.link,
                url=authorization_url,
            )
        )
        await interaction.response.send_message(
            "Use Congressus to verify your active Realtime membership.",
            view=view,
            ephemeral=True,
        )

    @app_commands.command(name="register", description="Register your Minecraft Java username")
    @app_commands.describe(username="Your Minecraft Java username")
    async def register(self, interaction: discord.Interaction, username: str) -> None:
        if interaction.guild_id != self.config.discord_guild_id:
            await interaction.response.send_message(
                "This command can only be used in the Realtime Discord server.", ephemeral=True
            )
            return
        if not is_valid_minecraft_username(username):
            await interaction.response.send_message(
                "Minecraft usernames must be 3–16 characters and contain only letters, numbers, or underscores.",
                ephemeral=True,
            )
            return

        try:
            member_link = await self.database.get_member(interaction.user.id)
            if member_link is None or not has_role(interaction, self.config.minecraft_role_id):
                await interaction.response.send_message(
                    "You must validate your active Realtime membership before registering a Minecraft username.",
                    ephemeral=True,
                )
                return
            member_link = await self.database.register_minecraft_username(
                interaction.user.id, username
            )
        except MinecraftUsernameAlreadySet as error:
            await interaction.response.send_message(
                f"You already registered `{error.username}`. Bestuur or Servercommissie must reset or change it.",
                ephemeral=True,
            )
            return
        except MinecraftUsernameClaimed:
            await interaction.response.send_message(
                "That Minecraft username is already registered by another member.", ephemeral=True
            )
            return
        except StorageError as error:
            await self._storage_error(interaction, error)
            return

        await interaction.response.send_message(
            f"Minecraft username `{member_link.minecraft_username}` registered. Server whitelisting is not enabled yet.",
            ephemeral=True,
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

    @whitelist.command(name="show", description="Show a member's stored Minecraft account link")
    @app_commands.describe(member="The Discord member to inspect")
    @minecraft_manager_only()
    async def whitelist_show(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        try:
            member_link = await self.database.get_member(member.id)
        except StorageError as error:
            await self._storage_error(interaction, error)
            return

        if member_link is None:
            await interaction.response.send_message(
                "That member has not completed Congressus validation.", ephemeral=True
            )
            return

        congressus_profile = member_link.congressus_name or "Name unavailable"
        if member_link.congressus_username:
            congressus_profile += f" (`{member_link.congressus_username}`)"
        embed = discord.Embed(title="Minecraft member link")
        embed.add_field(name="Discord member", value=member.mention, inline=False)
        embed.add_field(name="Congressus profile", value=congressus_profile, inline=False)
        embed.add_field(
            name="Minecraft username",
            value=(
                f"`{member_link.minecraft_username}`"
                if member_link.minecraft_username
                else "Not registered"
            ),
            inline=False,
        )
        embed.add_field(name="Verified at", value=member_link.verified_at, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @whitelist.command(name="reset", description="Clear a member's Minecraft username")
    @app_commands.describe(member="The Discord member whose username should be cleared")
    @minecraft_manager_only()
    async def whitelist_reset(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        try:
            changed = await self.database.reset_minecraft_username(member.id)
        except MemberNotValidated:
            await interaction.response.send_message(
                "That member has not completed Congressus validation.", ephemeral=True
            )
            return
        except StorageError as error:
            await self._storage_error(interaction, error)
            return

        message = (
            f"Cleared the stored Minecraft username for {member.mention}."
            if changed
            else f"{member.mention} does not have a Minecraft username registered."
        )
        await interaction.response.send_message(message, ephemeral=True)

    @whitelist.command(name="set", description="Set or replace a member's Minecraft username")
    @app_commands.describe(member="The validated Discord member", username="Minecraft Java username")
    @minecraft_manager_only()
    async def whitelist_set(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        username: str,
    ) -> None:
        if not is_valid_minecraft_username(username):
            await interaction.response.send_message(
                "Minecraft usernames must be 3–16 characters and contain only letters, numbers, or underscores.",
                ephemeral=True,
            )
            return
        try:
            member_link = await self.database.register_minecraft_username(
                member.id, username, replace=True
            )
        except MemberNotValidated:
            await interaction.response.send_message(
                "That member has not completed Congressus validation.", ephemeral=True
            )
            return
        except MinecraftUsernameClaimed:
            await interaction.response.send_message(
                "That Minecraft username is already registered by another member.", ephemeral=True
            )
            return
        except StorageError as error:
            await self._storage_error(interaction, error)
            return

        await interaction.response.send_message(
            f"Set {member.mention}'s Minecraft username to `{member_link.minecraft_username}`.",
            ephemeral=True,
        )

    @app_commands.command(name="commands", description="Show available Minecraft management commands")
    @minecraft_manager_only()
    async def commands_list(self, interaction: discord.Interaction) -> None:
        await self._management_placeholder(interaction)

    async def _storage_error(
        self, interaction: discord.Interaction, error: StorageError
    ) -> None:
        LOGGER.error(
            "Minecraft account storage operation failed (%s)", type(error).__name__
        )
        await interaction.response.send_message(
            "The account database is temporarily unavailable. Please try again later.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MinecraftCog(bot))
