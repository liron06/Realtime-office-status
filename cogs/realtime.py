import discord
from discord import app_commands
from discord.ext import commands

from config import Config
from utils.permissions import board_only


WELCOME_MARKER = "S.V. Realtime • Welkom"
MEMBERSHIP_URL = "https://www.sv-realtime.nl/lid-worden"
WEBSITE_URL = "https://sv-realtime.nl/"


def _channel_mention(channel_id: int | None, fallback: str) -> str:
    return f"<#{channel_id}>" if channel_id is not None else f"#{fallback}"


def build_welcome_embed(config: Config) -> discord.Embed:
    reclametime = _channel_mention(config.reclametime_channel_id, "reclametime")
    careertime = _channel_mention(config.careertime_channel_id, "carrièretime")
    commissions = _channel_mention(
        config.commission_channel_id, "commissiewerk-doen"
    )
    minecraft = _channel_mention(config.minecraft_channel_id, "minecraft")

    embed = discord.Embed(
        title="👋 Welkom bij S.V. Realtime!",
        description=(
            "De studievereniging voor ICT-studenten aan de Hanze.\n\n"
            "Op onze Discord vind je activiteiten, carrièrekansen, commissies "
            "en natuurlijk onze community."
        ),
        color=discord.Color.blue(),
    )
    embed.add_field(
        name="🎓 Nog geen lid?",
        value=(
            "Word lid van Realtime en doe mee met onze activiteiten en profiteer "
            "van de voordelen van het lidmaatschap.\n\n"
            f"[🌐 Lid worden]({MEMBERSHIP_URL}) · [🔗 Website]({WEBSITE_URL})"
        ),
        inline=False,
    )
    embed.add_field(
        name="📢 Op de hoogte blijven",
        value=(
            f"Onze activiteiten vind je in {reclametime}. Bedrijfs- en "
            f"carrièreactiviteiten vind je in {careertime}."
        ),
        inline=False,
    )
    embed.add_field(
        name="🤝 Actief worden?",
        value=(
            f"Wil je bijdragen aan de vereniging? Bekijk {commissions} voor onze "
            "commissies."
        ),
        inline=False,
    )
    embed.add_field(
        name="🎮 Minecraft",
        value=(
            "Realtime heeft een eigen Minecraft-server voor leden.\n\n"
            f"Ga naar {minecraft} en gebruik `/minecraft aanmelden` om je "
            "lidmaatschap te verifiëren en automatisch toegang te krijgen.\n\n"
            f"**Server**\n`{config.minecraft_server_host}`"
        ),
        inline=False,
    )
    embed.set_footer(text=WELCOME_MARKER)
    return embed


def build_welcome_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="Lid worden",
            emoji="🌐",
            style=discord.ButtonStyle.link,
            url=MEMBERSHIP_URL,
        )
    )
    view.add_item(
        discord.ui.Button(
            label="Website",
            emoji="🔗",
            style=discord.ButtonStyle.link,
            url=WEBSITE_URL,
        )
    )
    return view


class RealtimeCog(
    commands.GroupCog,
    group_name="realtime",
    group_description="Realtime-informatie en instellingen",
):
    setup_group = app_commands.Group(
        name="setup",
        description="Beheer permanente Realtime-berichten",
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config: Config = bot.config

    @setup_group.command(
        name="welkom",
        description="Plaats of vernieuw het officiële welkomstbericht",
    )
    @app_commands.guild_only()
    @board_only()
    async def welcome(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        if channel is None or not hasattr(channel, "history"):
            await interaction.response.send_message(
                "Gebruik dit commando in een tekstkanaal.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        embed = build_welcome_embed(self.config)
        view = build_welcome_view()
        existing = await self._find_welcome_message(channel)

        if existing is None:
            await channel.send(embed=embed, view=view)
            confirmation = "✅ Welkomstbericht is geplaatst."
        else:
            await existing.edit(embed=embed, view=view)
            confirmation = "✅ Welkomstbericht is bijgewerkt."

        await interaction.followup.send(confirmation, ephemeral=True)

    async def _find_welcome_message(self, channel) -> discord.Message | None:
        bot_user = self.bot.user
        if bot_user is None:
            return None
        async for message in channel.history(limit=100):
            if message.author.id != bot_user.id:
                continue
            if any(embed.footer.text == WELCOME_MARKER for embed in message.embeds):
                return message
        return None


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RealtimeCog(bot))
