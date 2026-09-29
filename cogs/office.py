import logging

import discord
from discord.ext import commands

from config import Config
from utils.permissions import has_management_permission


LOGGER = logging.getLogger(__name__)
OPEN_CHANNEL_NAME = "🟢 Kantoor: OPEN"
CLOSED_CHANNEL_NAME = "🔴 Kantoor: GESLOTEN"


class OfficeButtons(discord.ui.View):
    def __init__(self, config: Config) -> None:
        super().__init__(timeout=None)
        self.config = config

    async def _set_office_state(
        self,
        interaction: discord.Interaction,
        channel_name: str,
        confirmation: str,
    ) -> None:
        if not has_management_permission(interaction, self.config.office_manager_role_id):
            await interaction.response.send_message(
                "You do not have permission to manage the office status.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        channel = interaction.client.get_channel(self.config.office_voice_channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            LOGGER.error(
                "Office voice channel %d is not available in the bot cache",
                self.config.office_voice_channel_id,
            )
            await interaction.followup.send(
                "The office status channel is unavailable. Please contact an administrator.",
                ephemeral=True,
            )
            return

        if channel.name != channel_name:
            await channel.edit(name=channel_name, reason="Office status changed")
        await interaction.followup.send(confirmation, ephemeral=True)

    @discord.ui.button(label="Open", emoji="🟢", style=discord.ButtonStyle.green, custom_id="office_open")
    async def open_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._set_office_state(interaction, OPEN_CHANNEL_NAME, "🟢 Kantoor geopend.")

    @discord.ui.button(label="Gesloten", emoji="🔴", style=discord.ButtonStyle.red, custom_id="office_closed")
    async def close_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._set_office_state(interaction, CLOSED_CHANNEL_NAME, "🔴 Kantoor gesloten.")

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        LOGGER.error(
            "Office button %s failed",
            item.custom_id,
            exc_info=(type(error), error, error.__traceback__),
        )
        message = "Something went wrong while updating the office status. Please try again later."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class OfficeCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config: Config = bot.config
        self._control_message_sent = False
        self._view = OfficeButtons(self.config)

    async def cog_load(self) -> None:
        self.bot.add_view(self._view)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._control_message_sent:
            return
        channel = self.bot.get_channel(self.config.office_control_channel_id)
        if not isinstance(channel, discord.TextChannel):
            LOGGER.error(
                "Office control channel %d is not available in the bot cache",
                self.config.office_control_channel_id,
            )
            return
        await channel.send("🏢 **Kantoorstatus**", view=OfficeButtons(self.config))
        self._control_message_sent = True
        LOGGER.info("Posted office controls in channel %d", channel.id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(OfficeCog(bot))
