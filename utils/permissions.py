from collections.abc import Callable
from typing import Any

import discord
from discord import app_commands


class ManagementPermissionError(app_commands.CheckFailure):
    def __init__(self, area: str, *, message: str | None = None) -> None:
        super().__init__(message or f"You do not have permission to manage {area}.")


def has_role(interaction: discord.Interaction, role_id: int) -> bool:
    member = interaction.user
    return isinstance(member, discord.Member) and any(role.id == role_id for role in member.roles)


def has_management_permission(interaction: discord.Interaction, *role_ids: int) -> bool:
    member = interaction.user
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.administrator:
        return True
    allowed_role_ids = set(role_ids)
    return any(role.id in allowed_role_ids for role in member.roles)


def minecraft_manager_only() -> Callable[[Any], Any]:
    async def predicate(interaction: discord.Interaction) -> bool:
        config = interaction.client.config
        if has_management_permission(
            interaction,
            config.board_role_id,
            config.minecraft_manager_role_id,
        ):
            return True
        raise ManagementPermissionError("Minecraft")

    return app_commands.check(predicate)


def board_only() -> Callable[[Any], Any]:
    async def predicate(interaction: discord.Interaction) -> bool:
        config = interaction.client.config
        if has_management_permission(interaction, config.board_role_id):
            return True
        raise ManagementPermissionError(
            "Realtime-instellingen",
            message="Je hebt geen toestemming om Realtime-berichten te beheren.",
        )

    return app_commands.check(predicate)
