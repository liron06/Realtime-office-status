import logging
import re

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
from utils.permissions import minecraft_manager_only


LOGGER = logging.getLogger(__name__)
PLAYER_COUNT_PATTERN = re.compile(
    r"There are\s+(\d+)\s+of a max of\s+(\d+)\s+players online:\s*(.*)",
    re.IGNORECASE,
)


class MinecraftAccountModal(discord.ui.Modal, title="Minecraft-account koppelen"):
    username = discord.ui.TextInput(
        label="Minecraft-gebruikersnaam",
        placeholder="bijv. Player_name",
        min_length=3,
        max_length=16,
    )

    def __init__(self, cog: "MinecraftCog", discord_user_id: int) -> None:
        super().__init__()
        self.cog = cog
        self.discord_user_id = discord_user_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.discord_user_id:
            await interaction.response.send_message(
                "Deze koppeling is niet voor jouw Discord-account.", ephemeral=True
            )
            return
        await self.cog.register_from_interaction(interaction, str(self.username))


class MinecraftAccountLinkView(discord.ui.View):
    def __init__(self, cog: "MinecraftCog", discord_user_id: int) -> None:
        super().__init__(timeout=15 * 60)
        self.cog = cog
        self.discord_user_id = discord_user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.discord_user_id:
            return True
        await interaction.response.send_message(
            "Deze knop is niet voor jouw Discord-account.", ephemeral=True
        )
        return False

    @discord.ui.button(
        label="Minecraft-account koppelen",
        emoji="🎮",
        style=discord.ButtonStyle.primary,
        custom_id="minecraft_link_account",
    )
    async def link_account(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(
            MinecraftAccountModal(self.cog, self.discord_user_id)
        )


class MinecraftCog(
    commands.GroupCog,
    group_name="minecraft",
    group_description="Realtime Minecraft-server",
):
    whitelist = app_commands.Group(
        name="whitelist",
        description="Beheer gekoppelde Minecraft-accounts",
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config: Config = bot.config
        self.database: DatabaseService = bot.database
        self.service = MinecraftService(self.config, self.database)
        congressus_cog = bot.get_cog("CongressusCog")
        if not isinstance(congressus_cog, CongressusCog):
            raise RuntimeError("CongressusCog must be loaded before MinecraftCog")
        self.congressus = congressus_cog.service
        self.congressus.set_verified_callback(self.send_post_verification_prompt)

    async def cog_load(self) -> None:
        await self.service.start()

    async def cog_unload(self) -> None:
        self.congressus.set_verified_callback(None)
        await self.service.close()

    @app_commands.command(
        name="aanmelden", description="Aanmelden voor de Realtime Minecraft-server"
    )
    async def aanmelden(self, interaction: discord.Interaction) -> None:
        await self.show_onboarding(interaction)

    async def show_onboarding(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id != self.config.discord_guild_id:
            await interaction.response.send_message(
                "Dit commando werkt alleen in de Realtime Discord-server.", ephemeral=True
            )
            return

        try:
            member_link = await self.database.get_member(interaction.user.id)
        except StorageError as error:
            await self._storage_error(interaction, error)
            return

        if member_link is not None and member_link.minecraft_username is not None:
            await self._send(
                interaction,
                embed=self._already_registered_embed(member_link.minecraft_username),
            )
            return

        if member_link is not None:
            await self._send(
                interaction,
                embed=self._link_account_embed(already_verified=True),
                view=MinecraftAccountLinkView(self, interaction.user.id),
            )
            return

        authorization_url = await self.congressus.create_authorization_url(interaction.user.id)
        view = discord.ui.View(timeout=10 * 60)
        view.add_item(
            discord.ui.Button(
                label="Verifiëren via Congressus",
                style=discord.ButtonStyle.link,
                url=authorization_url,
            )
        )
        embed = discord.Embed(
            title="🎮 Realtime Minecraft",
            description=(
                "Om toegang te krijgen tot de Minecraft-server moet je eerst je "
                "Realtime-lidmaatschap verifiëren."
            ),
        )
        await self._send(interaction, embed=embed, view=view)

    async def send_post_verification_prompt(self, discord_user_id: int) -> None:
        user = self.bot.get_user(discord_user_id)
        if user is None:
            user = await self.bot.fetch_user(discord_user_id)
        await user.send(
            embed=self._link_account_embed(already_verified=False),
            view=MinecraftAccountLinkView(self, discord_user_id),
        )

    async def register_from_interaction(
        self, interaction: discord.Interaction, username: str
    ) -> None:
        if not is_valid_minecraft_username(username):
            await interaction.response.send_message(
                "Een Minecraft-gebruikersnaam heeft 3–16 tekens en bevat alleen letters, cijfers of underscores.",
                ephemeral=True,
            )
            return

        try:
            member_link = await self.database.get_member(interaction.user.id)
            if member_link is None or not await self._has_validated_role(interaction.user.id):
                await interaction.response.send_message(
                    "Verifieer eerst je Realtime-lidmaatschap via `/minecraft aanmelden`.",
                    ephemeral=True,
                )
                return
            await interaction.response.defer(ephemeral=True)
            member_link = await self.service.register(interaction.user.id, username)
        except MinecraftUsernameAlreadySet as error:
            await self._send(
                interaction,
                f"Je hebt al `{error.username}` gekoppeld. Bestuur of Servercommissie kan dit wijzigen.",
            )
            return
        except MinecraftUsernameClaimed:
            await self._send(
                interaction,
                "Deze Minecraft-gebruikersnaam is al door iemand anders gekoppeld.",
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
                f"`{username}` is gekoppeld, maar synchronisatie met de Minecraft-whitelist is mislukt. Neem contact op met Bestuur of Servercommissie.",
            )
            return
        except StorageError as error:
            await self._storage_error(interaction, error)
            return

        await self._send(interaction, embed=self._success_embed(member_link.minecraft_username))

    @app_commands.command(
        name="status", description="Bekijk de status van de Minecraft-server"
    )
    async def status(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Dit commando werkt alleen in een Discord-server.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            payload = await self.service.api.status()
        except MinecraftApiError as error:
            await self._api_error(interaction, error)
            return
        await self._send(interaction, _format_status(payload, self.config.minecraft_server_host))

    @app_commands.command(name="players", description="Bekijk wie er online is")
    async def players(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Dit commando werkt alleen in een Discord-server.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            payload = await self.service.api.players()
        except MinecraftApiError as error:
            await self._api_error(interaction, error)
            return
        await self._send(interaction, _format_players(payload))

    @app_commands.command(name="start", description="Start de Minecraft-server")
    @minecraft_manager_only()
    async def start(self, interaction: discord.Interaction) -> None:
        await self._server_operation(
            interaction, self.service.api.server_start, "Minecraft server start requested."
        )

    @app_commands.command(name="stop", description="Stop de Minecraft-server")
    @minecraft_manager_only()
    async def stop(self, interaction: discord.Interaction) -> None:
        await self._server_operation(
            interaction, self.service.api.server_stop, "Minecraft server stop requested."
        )

    @app_commands.command(name="restart", description="Herstart de Minecraft-server")
    @minecraft_manager_only()
    async def restart(self, interaction: discord.Interaction) -> None:
        await self._server_operation(
            interaction, self.service.api.server_restart, "Minecraft server restart requested."
        )

    @whitelist.command(name="show", description="Bekijk het gekoppelde Minecraft-account")
    @app_commands.describe(member="Het Discord-lid dat je wilt bekijken")
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

    @whitelist.command(name="reset", description="Verwijder een gekoppeld Minecraft-account")
    @app_commands.describe(member="Het Discord-lid waarvan je de koppeling verwijdert")
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

    @whitelist.command(name="set", description="Stel een Minecraft-account in")
    @app_commands.describe(
        member="Het geverifieerde Discord-lid",
        username="Minecraft Java-gebruikersnaam",
    )
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

    @whitelist.command(name="sync", description="Synchroniseer een account met de whitelist")
    @app_commands.describe(member="Het Discord-lid dat je wilt synchroniseren")
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

    async def _has_validated_role(self, discord_user_id: int) -> bool:
        guild = self.bot.get_guild(self.config.discord_guild_id)
        if guild is None:
            return False
        member = guild.get_member(discord_user_id)
        if member is None:
            try:
                member = await guild.fetch_member(discord_user_id)
            except discord.HTTPException:
                return False
        return any(role.id == self.config.minecraft_role_id for role in member.roles)

    def _link_account_embed(self, *, already_verified: bool) -> discord.Embed:
        prefix = (
            "Je Realtime-lidmaatschap is al geverifieerd."
            if already_verified
            else "Verificatie geslaagd ✅\nJe Realtime-lidmaatschap is geverifieerd."
        )
        return discord.Embed(
            title="🎮 Realtime Minecraft",
            description=f"{prefix}\n\nKoppel nu je Minecraft-account.",
        )

    def _already_registered_embed(self, username: str) -> discord.Embed:
        return discord.Embed(
            title="🎮 Realtime Minecraft",
            description=(
                "Je bent al aangemeld voor Realtime Minecraft.\n\n"
                f"Minecraft-account: `{username}`\n"
                f"Server: `{self.config.minecraft_server_host}`\n\n"
                "Minecraft Java Edition"
            ),
        )

    def _success_embed(self, username: str) -> discord.Embed:
        return discord.Embed(
            title="✅ Klaar om te spelen!",
            description=(
                f"Je Minecraft-account `{username}` is gekoppeld en staat op de whitelist.\n\n"
                f"Server: `{self.config.minecraft_server_host}`\n"
                "Minecraft Java Edition\n\n"
                "Voeg de server toe in Multiplayer en je kunt spelen."
            ),
        )

    async def _storage_error(
        self, interaction: discord.Interaction, error: StorageError
    ) -> None:
        LOGGER.error(
            "Minecraft account storage operation failed (%s)", type(error).__name__
        )
        await self._send(
            interaction,
            "De accountdatabase is tijdelijk niet beschikbaar. Probeer het later opnieuw.",
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
    async def _send(
        interaction: discord.Interaction, message: str | None = None, **kwargs
    ) -> None:
        kwargs.setdefault("ephemeral", True)
        if interaction.response.is_done():
            await interaction.followup.send(message, **kwargs)
        else:
            await interaction.response.send_message(message, **kwargs)


def _format_status(payload: object, server_host: str) -> str:
    running = _find_value(payload, "running", "online", "server_online")
    status = _find_value(payload, "status", "state")
    health = _find_value(payload, "health", "healthy")

    online = _as_bool(running)
    if online is None and isinstance(status, str):
        lowered = status.casefold()
        if lowered in {"online", "running", "healthy", "ok", "up"}:
            online = True
        elif lowered in {"offline", "stopped", "down", "unhealthy"}:
            online = False

    if online is True:
        server_line = "🟢 Server online"
    elif online is False:
        server_line = "🔴 Server offline"
    else:
        server_line = "🟡 Serverstatus onbekend"

    health_value = health if health is not None else status
    health_line = _health_line(health_value)
    lines = ["**🎮 Realtime Minecraft**", server_line]
    if health_line:
        lines.append(health_line)
    lines.append(f"🌐 `{server_host}`")
    return "\n".join(lines)


def _format_players(payload: object) -> str:
    names = (
        [value for value in payload if isinstance(value, str)]
        if isinstance(payload, list)
        else []
    )
    player_value = _find_value(payload, "players", "player_names", "online_players")
    if isinstance(player_value, list):
        names = [value for value in player_value if isinstance(value, str)]

    online = _as_int(_find_value(payload, "online", "count", "player_count"))
    maximum = _as_int(_find_value(payload, "max", "maximum", "max_players"))

    for text in _string_values(payload):
        match = PLAYER_COUNT_PATTERN.search(text)
        if match:
            online = int(match.group(1))
            maximum = int(match.group(2))
            if not names and match.group(3).strip():
                names = [name.strip() for name in match.group(3).split(",") if name.strip()]
            break

    if online is None:
        online = len(names)
    maximum_text = str(maximum) if maximum is not None else "?"
    lines = [f"**👥 Spelers online: {online} / {maximum_text}**"]
    if names:
        lines.append(", ".join(f"`{name}`" for name in names[:30]))
    return "\n".join(lines)


def _find_value(payload: object, *keys: str) -> object | None:
    wanted = {key.casefold() for key in keys}
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).casefold() in wanted:
                return value
        for value in payload.values():
            found = _find_value(value, *keys)
            if found is not None:
                return found
    return None


def _string_values(payload: object):
    if isinstance(payload, str):
        yield payload
    elif isinstance(payload, dict):
        for value in payload.values():
            yield from _string_values(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from _string_values(value)


def _as_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.casefold() in {"true", "yes", "online", "running", "up"}:
            return True
        if value.casefold() in {"false", "no", "offline", "stopped", "down"}:
            return False
    return None


def _as_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _health_line(value: object) -> str | None:
    if isinstance(value, bool):
        return "❤️ Status: gezond" if value else "💔 Status: storing"
    if isinstance(value, str):
        lowered = value.casefold()
        if lowered in {"healthy", "ok", "online", "running", "up"}:
            return "❤️ Status: gezond"
        if lowered in {"unhealthy", "error", "offline", "down"}:
            return "💔 Status: storing"
    return None


def _api_error_message(error: MinecraftApiError) -> str:
    if isinstance(error, MinecraftApiTimeout):
        return "De Minecraft-service reageerde niet op tijd. Probeer het later opnieuw."
    if isinstance(error, MinecraftApiUnavailable):
        return "De Minecraft-service is momenteel niet bereikbaar. Probeer het later opnieuw."
    if isinstance(error, MinecraftApiUnauthorized):
        return "De Minecraft-koppeling is verkeerd geconfigureerd. Neem contact op met een beheerder."
    if isinstance(error, MinecraftMalformedResponse):
        return "De Minecraft-service gaf een ongeldig antwoord. Neem contact op met een beheerder."
    if isinstance(error, MinecraftOperationFailed):
        return "De Minecraft-actie is mislukt. Probeer het later opnieuw."
    return "De Minecraft-koppeling kon deze actie niet afronden."


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MinecraftCog(bot))
