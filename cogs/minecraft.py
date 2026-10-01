import json
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
from services.minecraft import (
    MinecraftApiError,
    MinecraftApiTimeout,
    MinecraftApiUnauthorized,
    MinecraftApiUnavailable,
    MinecraftMalformedResponse,
    MinecraftOperationFailed,
    MinecraftService,
    RegistrationSyncFailed,
    WhitelistChangeFailed,
    is_valid_minecraft_username,
)
from utils.permissions import has_role, minecraft_manager_only


LOGGER = logging.getLogger(__name__)


class MinecraftCog(commands.GroupCog, group_name="minecraft", group_description="Minecraft tools"):
    whitelist = app_commands.Group(
        name="whitelist",
        description="Manage validated Minecraft member accounts",
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.config: Config = bot.config
        self.database: DatabaseService = bot.database
        self.service = MinecraftService(self.config, self.database)
        congressus_cog = bot.get_cog("CongressusCog")
        if not isinstance(congressus_cog, CongressusCog):
            raise RuntimeError("CongressusCog must be loaded before MinecraftCog")
        self.congressus = congressus_cog.service

    async def cog_load(self) -> None:
        await self.service.start()

    async def cog_unload(self) -> None:
        await self.service.close()

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
            await interaction.response.defer(ephemeral=True)
            member_link = await self.service.register(interaction.user.id, username)
        except MinecraftUsernameAlreadySet as error:
            await self._send(
                interaction,
                f"You already registered `{error.username}`. Bestuur or Servercommissie must reset or change it.",
            )
            return
        except MinecraftUsernameClaimed:
            await self._send(
                interaction,
                "That Minecraft username is already registered by another member.", ephemeral=True
            )
            return
        except RegistrationSyncFailed as error:
            LOGGER.warning(
                "Minecraft registration stored but whitelist sync failed for Discord user %d (%s)",
                interaction.user.id,
                type(error.cause).__name__,
            )
            await self._send(
                interaction,
                f"`{username}` is registered to you, but Minecraft whitelist synchronization failed. An administrator can retry it with `/minecraft whitelist sync`.",
            )
            return
        except StorageError as error:
            await self._storage_error(interaction, error)
            return

        await self._send(
            interaction,
            f"Minecraft username `{member_link.minecraft_username}` registered and whitelisted.",
        )

    @app_commands.command(name="status", description="Show the Minecraft server status")
    async def status(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used by server members.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            payload = await self.service.api.status()
        except MinecraftApiError as error:
            await self._api_error(interaction, error)
            return
        await self._send(interaction, _format_payload("Minecraft server status", payload))

    @app_commands.command(name="players", description="Show the online Minecraft players")
    async def players(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used by server members.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            payload = await self.service.api.players()
        except MinecraftApiError as error:
            await self._api_error(interaction, error)
            return
        await self._send(interaction, _format_payload("Online Minecraft players", payload))

    @app_commands.command(name="start", description="Start the Minecraft server")
    @minecraft_manager_only()
    async def start(self, interaction: discord.Interaction) -> None:
        await self._server_operation(
            interaction, self.service.api.server_start, "Minecraft server start requested."
        )

    @app_commands.command(name="stop", description="Stop the Minecraft server")
    @minecraft_manager_only()
    async def stop(self, interaction: discord.Interaction) -> None:
        await self._server_operation(
            interaction, self.service.api.server_stop, "Minecraft server stop requested."
        )

    @app_commands.command(name="restart", description="Restart the Minecraft server")
    @minecraft_manager_only()
    async def restart(self, interaction: discord.Interaction) -> None:
        await self._server_operation(
            interaction, self.service.api.server_restart, "Minecraft server restart requested."
        )

    @whitelist.command(name="show", description="Show a member's stored Minecraft account link")
    @app_commands.describe(member="The Discord member to inspect")
    @minecraft_manager_only()
    async def whitelist_show(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            member_link = await self.database.get_member(member.id)
        except StorageError as error:
            await self._storage_error(interaction, error)
            return

        if member_link is None:
            await self._send(
                interaction, "That member has not completed Congressus validation."
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
        await interaction.followup.send(embed=embed, ephemeral=True)

    @whitelist.command(name="reset", description="Clear a member's Minecraft username")
    @app_commands.describe(member="The Discord member whose username should be cleared")
    @minecraft_manager_only()
    async def whitelist_reset(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            removed_username = await self.service.reset(member.id)
        except MemberNotValidated:
            await self._send(
                interaction, "That member has not completed Congressus validation."
            )
            return
        except MinecraftApiError as error:
            LOGGER.warning(
                "Minecraft whitelist removal failed before reset for Discord user %d (%s)",
                member.id,
                type(error).__name__,
            )
            await self._send(
                interaction,
                "The username was not reset because it could not be safely removed from the Minecraft whitelist. "
                f"The stored account link is unchanged. {_api_error_message(error)}",
            )
            return
        except StorageError as error:
            await self._storage_error(interaction, error)
            return

        message = (
            f"Removed `{removed_username}` from the whitelist and cleared it for {member.mention}."
            if removed_username
            else f"{member.mention} does not have a Minecraft username registered."
        )
        await self._send(interaction, message)

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
        await interaction.response.defer(ephemeral=True)
        try:
            member_link = await self.service.set_username(member.id, username)
        except MemberNotValidated:
            await self._send(
                interaction, "That member has not completed Congressus validation."
            )
            return
        except MinecraftUsernameClaimed:
            await self._send(
                interaction, "That Minecraft username is already registered by another member."
            )
            return
        except WhitelistChangeFailed as error:
            LOGGER.warning(
                "Minecraft whitelist change failed for Discord user %d (%s, rollback_failed=%s)",
                member.id,
                type(error.cause).__name__,
                error.rollback_failed,
            )
            message = (
                "The username change failed and the old database mapping was kept. Restoring the old whitelist entry also failed; an administrator should run whitelist sync after the API recovers."
                if error.rollback_failed
                else "The username change failed and the database mapping was not changed. Any previous whitelist entry was preserved or restored."
            )
            await self._send(interaction, message)
            return
        except MinecraftApiError as error:
            await self._send(
                interaction,
                f"The username change was not completed and the database mapping is unchanged. {_api_error_message(error)}",
            )
            return
        except StorageError as error:
            await self._storage_error(interaction, error)
            return

        await self._send(
            interaction,
            f"Set {member.mention}'s Minecraft username to `{member_link.minecraft_username}`.",
        )

    @whitelist.command(name="sync", description="Ensure a member's username is whitelisted")
    @app_commands.describe(member="The Discord member to synchronize")
    @minecraft_manager_only()
    async def whitelist_sync(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            username = await self.service.sync(member.id)
        except MemberNotValidated:
            await self._send(
                interaction, "That member has not completed Congressus validation."
            )
            return
        except MinecraftApiError as error:
            await self._api_error(interaction, error)
            return
        except StorageError as error:
            await self._storage_error(interaction, error)
            return

        if username is None:
            await self._send(interaction, "That member has no Minecraft username to synchronize.")
            return
        await self._send(interaction, f"Ensured `{username}` is on the Minecraft whitelist.")

    @app_commands.command(name="commands", description="Show available Minecraft management commands")
    @minecraft_manager_only()
    async def commands_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "Available management commands: start, stop, restart, and whitelist show/reset/set/sync.",
            ephemeral=True,
        )

    async def _storage_error(
        self, interaction: discord.Interaction, error: StorageError
    ) -> None:
        LOGGER.error(
            "Minecraft account storage operation failed (%s)", type(error).__name__
        )
        await self._send(
            interaction,
            "The account database is temporarily unavailable. Please try again later.",
        )

    async def _api_error(
        self, interaction: discord.Interaction, error: MinecraftApiError
    ) -> None:
        await self._send(interaction, _api_error_message(error))

    async def _server_operation(
        self,
        interaction: discord.Interaction,
        operation,
        success_message: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            await operation()
        except MinecraftApiError as error:
            await self._api_error(interaction, error)
            return
        await self._send(interaction, success_message)

    @staticmethod
    async def _send(interaction: discord.Interaction, message: str, **kwargs) -> None:
        kwargs.setdefault("ephemeral", True)
        if interaction.response.is_done():
            await interaction.followup.send(message, **kwargs)
        else:
            await interaction.response.send_message(message, **kwargs)


def _format_payload(title: str, payload: object) -> str:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(rendered) > 1750:
        rendered = rendered[:1747] + "..."
    return f"**{title}**\n```json\n{rendered}\n```"


def _api_error_message(error: MinecraftApiError) -> str:
    if isinstance(error, MinecraftApiTimeout):
        return "The Minecraft API timed out. Please try again later."
    if isinstance(error, MinecraftApiUnavailable):
        return "The Minecraft API is currently unavailable. Please try again later."
    if isinstance(error, MinecraftApiUnauthorized):
        return "Minecraft API authentication is misconfigured. Please contact an administrator."
    if isinstance(error, MinecraftMalformedResponse):
        return "The Minecraft API returned an invalid response. Please contact an administrator."
    if isinstance(error, MinecraftOperationFailed):
        return "The Minecraft operation failed. Please try again later."
    return "The Minecraft integration could not complete that request."


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MinecraftCog(bot))
