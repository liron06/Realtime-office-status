import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

from cogs.minecraft import MinecraftCog
from cogs.realtime import (
    MEMBERSHIP_URL,
    WEBSITE_URL,
    RealtimeCog,
    build_welcome_embed,
    build_welcome_view,
)
from config import Config
from utils.permissions import ManagementPermissionError


def make_config() -> Config:
    return Config(
        discord_token="test-token",
        office_voice_channel_id=1,
        office_control_channel_id=2,
        office_manager_role_id=3,
        minecraft_manager_role_id=4,
        board_role_id=5,
        discord_guild_id=6,
        minecraft_role_id=7,
        congressus_client_id="test-client",
        congressus_client_secret="test-secret",
        congressus_base_url="https://www.sv-realtime.nl",
        congressus_redirect_uri="https://auth.example.test/congressus/callback",
        minecraft_api_url="http://10.77.0.2:8081",
        minecraft_api_token="test-api-token",
        minecraft_server_host="minecraft.example.test",
        database_path=Path("unused.db"),
        reclametime_channel_id=101,
        careertime_channel_id=102,
        commission_channel_id=103,
        minecraft_channel_id=104,
    )


def make_member(*role_ids: int, administrator: bool = False):
    member = Mock(spec=discord.Member)
    member.roles = [SimpleNamespace(id=role_id) for role_id in role_ids]
    member.guild_permissions = SimpleNamespace(administrator=administrator)
    return member


class CaptureResponse:
    def __init__(self) -> None:
        self.deferred = None
        self.sent = None

    async def defer(self, **kwargs) -> None:
        self.deferred = kwargs

    async def send_message(self, *args, **kwargs) -> None:
        self.sent = (args, kwargs)


class CaptureFollowup:
    def __init__(self) -> None:
        self.sent = None

    async def send(self, *args, **kwargs) -> None:
        self.sent = (args, kwargs)


class FakeMessage:
    def __init__(self, author_id: int, embed: discord.Embed, view: discord.ui.View):
        self.author = SimpleNamespace(id=author_id)
        self.embeds = [embed]
        self.view = view
        self.edit_count = 0

    async def edit(self, *, embed: discord.Embed, view: discord.ui.View) -> None:
        self.embeds = [embed]
        self.view = view
        self.edit_count += 1


class FakeChannel:
    def __init__(self, bot_user_id: int) -> None:
        self.bot_user_id = bot_user_id
        self.messages = []
        self.send_count = 0

    async def send(self, *, embed: discord.Embed, view: discord.ui.View) -> FakeMessage:
        message = FakeMessage(self.bot_user_id, embed, view)
        self.messages.insert(0, message)
        self.send_count += 1
        return message

    async def history(self, *, limit: int):
        for message in self.messages[:limit]:
            yield message


class RealtimeWelcomeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.config = make_config()
        self.bot = SimpleNamespace(config=self.config, user=SimpleNamespace(id=999))
        self.cog = RealtimeCog(self.bot)

    def welcome_command(self):
        root = self.cog.__cog_app_commands_group__
        setup = next(command for command in root.commands if command.name == "setup")
        return next(command for command in setup.commands if command.name == "welkom")

    def interaction(self, channel=None, user=None):
        return SimpleNamespace(
            channel=channel or FakeChannel(self.bot.user.id),
            guild=SimpleNamespace(id=self.config.discord_guild_id),
            user=user or make_member(self.config.board_role_id),
            client=SimpleNamespace(config=self.config),
            response=CaptureResponse(),
            followup=CaptureFollowup(),
        )

    def test_realtime_setup_welkom_is_registered(self):
        command = self.welcome_command()
        self.assertEqual(command.qualified_name, "realtime setup welkom")

    async def test_only_administrator_and_board_pass_permission_check(self):
        command = self.welcome_command()

        for member in (
            make_member(administrator=True),
            make_member(self.config.board_role_id),
        ):
            interaction = self.interaction(user=member)
            for predicate in command.checks:
                self.assertTrue(await predicate(interaction))

        for member in (
            make_member(),
            make_member(self.config.minecraft_manager_role_id),
        ):
            interaction = self.interaction(user=member)
            errors = []
            for predicate in command.checks:
                try:
                    await predicate(interaction)
                except ManagementPermissionError as error:
                    errors.append(error)
            self.assertEqual(len(errors), 1)

    def test_embed_and_buttons_contain_configured_content(self):
        embed = build_welcome_embed(self.config)
        content = "\n".join(
            [embed.description or "", *(field.value for field in embed.fields)]
        )
        view = build_welcome_view()

        self.assertIn(MEMBERSHIP_URL, content)
        self.assertIn(WEBSITE_URL, content)
        self.assertIn(self.config.minecraft_server_host, content)
        self.assertIn("<#101>", content)
        self.assertIn("<#102>", content)
        self.assertIn("<#103>", content)
        self.assertIn("<#104>", content)
        self.assertEqual(
            {button.url for button in view.children},
            {MEMBERSHIP_URL, WEBSITE_URL},
        )

    async def test_repeated_setup_updates_one_public_message(self):
        channel = FakeChannel(self.bot.user.id)
        command = self.welcome_command()

        first = self.interaction(channel=channel)
        await command.callback(self.cog, first)
        second = self.interaction(channel=channel)
        await command.callback(self.cog, second)

        self.assertEqual(channel.send_count, 1)
        self.assertEqual(len(channel.messages), 1)
        self.assertEqual(channel.messages[0].edit_count, 1)
        self.assertTrue(first.response.deferred["ephemeral"])
        self.assertTrue(first.followup.sent[1]["ephemeral"])
        self.assertIn("geplaatst", first.followup.sent[0][0])
        self.assertIn("bijgewerkt", second.followup.sent[0][0])

    def test_existing_minecraft_command_structure_is_intact(self):
        root = MinecraftCog.__new__(MinecraftCog).__cog_app_commands_group__
        commands = {command.name: command for command in root.commands}

        self.assertTrue({"aanmelden", "status", "players"}.issubset(commands))
        self.assertTrue({"start", "stop", "restart", "whitelist"}.issubset(commands))
        self.assertEqual(
            {command.name for command in commands["whitelist"].commands},
            {"show", "set", "reset", "sync"},
        )


if __name__ == "__main__":
    unittest.main()
