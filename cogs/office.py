import asyncio
import logging

import discord
from discord.ext import commands

from config import Config
from utils.permissions import has_management_permission


LOGGER = logging.getLogger(__name__)
OPEN_CHANNEL_NAME = "🟢 Kantoor: OPEN"
CLOSED_CHANNEL_NAME = "🔴 Kantoor: GESLOTEN"
PANEL_TITLE = "🏢 Realtime Kantoor"
PANEL_DESCRIPTION = "Gebruik de knoppen om de zichtbare kantoorstatus aan te passen."
PANEL_BUTTON_IDS = {"office_open", "office_closed"}
PANEL_HISTORY_LIMIT = 100


def office_panel_embed() -> discord.Embed:
    return discord.Embed(title=PANEL_TITLE, description=PANEL_DESCRIPTION)


def is_office_panel(message: discord.Message, bot_user_id: int) -> bool:
    if message.author.id != bot_user_id:
        return False

    custom_ids = {
        component.custom_id
        for row in message.components
        for component in row.children
        if getattr(component, "custom_id", None) is not None
    }
    return PANEL_BUTTON_IDS.issubset(custom_ids)


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
        if not has_management_permission(
            interaction,
            self.config.board_role_id,
            self.config.office_manager_role_id,
        ):
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
        self._panel_ready = False
        self._panel_lock = asyncio.Lock()
        self._view = OfficeButtons(self.config)

    async def cog_load(self) -> None:
        self.bot.add_view(self._view)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._panel_ready:
            return

        async with self._panel_lock:
            if self._panel_ready:
                return

            channel = self.bot.get_channel(self.config.office_control_channel_id)
            if not isinstance(channel, discord.TextChannel):
                LOGGER.error(
                    "Office control channel %d is not available in the bot cache",
                    self.config.office_control_channel_id,
                )
                return

            try:
                panels = [
                    message
                    async for message in channel.history(limit=PANEL_HISTORY_LIMIT)
                    if self.bot.user is not None and is_office_panel(message, self.bot.user.id)
                ]

                if panels:
                    panel = panels[0]
                    if (
                        panel.content
                        or len(panel.embeds) != 1
                        or panel.embeds[0].title != PANEL_TITLE
                        or panel.embeds[0].description != PANEL_DESCRIPTION
                    ):
                        await panel.edit(content=None, embed=office_panel_embed())

                    for duplicate in panels[1:]:
                        await duplicate.delete()

                    LOGGER.info(
                        "Found existing Office panel %d in channel %d; removed %d duplicate(s)",
                        panel.id,
                        channel.id,
                        len(panels) - 1,
                    )
                else:
                    panel = await channel.send(embed=office_panel_embed(), view=self._view)
                    LOGGER.info("Created new Office panel %d in channel %d", panel.id, channel.id)

                self._panel_ready = True
            except discord.HTTPException:
                LOGGER.exception(
                    "Could not initialize the Office panel in channel %d", channel.id
                )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(OfficeCog(bot))
