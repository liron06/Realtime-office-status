import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
VOICE_CHANNEL_ID = int(os.getenv("OFFICE_VOICE_CHANNEL_ID"))
CONTROL_CHANNEL_ID = int(os.getenv("OFFICE_CONTROL_CHANNEL_ID"))

bot = commands.Bot(
    command_prefix="!",
    intents=discord.Intents.default()
)


class OfficeButtons(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Open",
        emoji="🟢",
        style=discord.ButtonStyle.green,
        custom_id="office_open"
    )
    async def open_button(self, interaction, button):
        await interaction.response.defer(ephemeral=True)

        channel = bot.get_channel(VOICE_CHANNEL_ID)

        if channel.name != "🟢 Kantoor: OPEN":
            await channel.edit(name="🟢 Kantoor: OPEN")

        await interaction.followup.send(
            "🟢 Kantoor geopend.",
            ephemeral=True
        )

    @discord.ui.button(
        label="Gesloten",
        emoji="🔴",
        style=discord.ButtonStyle.red,
        custom_id="office_closed"
    )
    async def close_button(self, interaction, button):
        await interaction.response.defer(ephemeral=True)

        channel = bot.get_channel(VOICE_CHANNEL_ID)

        if channel.name != "🔴 Kantoor: GESLOTEN":
            await channel.edit(name="🔴 Kantoor: GESLOTEN")

        await interaction.followup.send(
            "🔴 Kantoor gesloten.",
            ephemeral=True
        )


@bot.event
async def on_ready():
    bot.add_view(OfficeButtons())

    print(f"Bot online als {bot.user}")

    channel = bot.get_channel(CONTROL_CHANNEL_ID)

    await channel.send(
        "🏢 **Kantoorstatus**",
        view=OfficeButtons()
    )


bot.run(TOKEN)