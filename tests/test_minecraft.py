import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import discord

from cogs.congressus import CongressusCog
from cogs.minecraft import (
    MinecraftAccountLinkView,
    MinecraftAccountModal,
    MinecraftCog,
    _format_players,
    _format_status,
)
from cogs.office import CLOSED_CHANNEL_NAME, OfficeButtons
from config import Config
from services.database import DatabaseService
from services.minecraft import (
    MinecraftApiClient,
    MinecraftMalformedResponse,
    MinecraftApiTimeout,
    MinecraftApiUnauthorized,
    MinecraftApiUnavailable,
    MinecraftService,
    RegistrationSyncFailed,
    WhitelistChangeFailed,
    is_valid_minecraft_username,
)
from utils.permissions import ManagementPermissionError


def make_config(database_path: Path) -> Config:
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
        minecraft_api_token="never-log-this-token",
        minecraft_server_host="mc.sv-realtime-gaming.nl",
        database_path=database_path,
    )


class FakeResponse:
    def __init__(self, status=200, payload=None, json_error=None):
        self.status = status
        self.payload = payload
        self.json_error = json_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False

    async def json(self, **_kwargs):
        if self.json_error is not None:
            raise self.json_error
        return self.payload


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.closed = False
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.error is not None:
            raise self.error
        return self.response


class CaptureResponse:
    def __init__(self):
        self.sent = None
        self.modal = None
        self._done = False

    async def send_message(self, *args, **kwargs):
        self.sent = (args, kwargs)
        self._done = True

    async def defer(self, **_kwargs):
        self._done = True

    async def send_modal(self, modal):
        self.modal = modal
        self._done = True

    def is_done(self):
        return self._done


class CaptureFollowup:
    def __init__(self):
        self.sent = None

    async def send(self, *args, **kwargs):
        self.sent = (args, kwargs)


class MinecraftApiClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_authentication_header_is_used(self):
        client = MinecraftApiClient("http://10.77.0.2:8081", "secret-token")
        session = FakeSession(FakeResponse(payload={"running": True}))
        client._session = session

        await client.status()

        _, url, kwargs = session.calls[0]
        self.assertEqual(url, "http://10.77.0.2:8081/status")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret-token")

    async def test_token_is_not_exposed_in_authentication_errors(self):
        token = "extremely-sensitive-token"
        client = MinecraftApiClient("http://10.77.0.2:8081", token)
        client._session = FakeSession(FakeResponse(status=401))

        with self.assertLogs("services.minecraft", level="ERROR") as logs:
            with self.assertRaises(MinecraftApiUnauthorized) as raised:
                await client.status()

        self.assertNotIn(token, str(raised.exception))
        self.assertNotIn(token, "\n".join(logs.output))

    async def test_unavailable_and_timeout_are_distinguished(self):
        client = MinecraftApiClient("http://10.77.0.2:8081", "secret")
        client._session = FakeSession(error=aiohttp.ClientConnectionError())
        with self.assertRaises(MinecraftApiUnavailable):
            await client.status()

        client._session = FakeSession(error=asyncio.TimeoutError())
        with self.assertRaises(MinecraftApiTimeout):
            await client.status()

    async def test_malformed_json_is_rejected(self):
        client = MinecraftApiClient("http://10.77.0.2:8081", "secret")
        client._session = FakeSession(FakeResponse(json_error=ValueError("invalid")))
        with self.assertRaises(MinecraftMalformedResponse):
            await client.status()


class MinecraftRegistrationWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        path = Path(self.temporary_directory.name) / "realtime.db"
        self.database = DatabaseService(path)
        await self.database.initialize()
        self.config = make_config(path)
        await self.database.link_member(100, "congressus-100", "member", "Member")
        self.api = SimpleNamespace(
            start=AsyncMock(),
            close=AsyncMock(),
            whitelist_add=AsyncMock(),
            whitelist_remove=AsyncMock(),
        )
        self.service = MinecraftService(self.config, self.database, self.api)

    async def asyncTearDown(self):
        self.temporary_directory.cleanup()

    async def test_register_adds_whitelist_entry(self):
        member = await self.service.register(100, "PlayerOne")
        self.assertEqual(member.minecraft_username, "PlayerOne")
        self.api.whitelist_add.assert_awaited_once_with("PlayerOne")


class MinecraftOnboardingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        path = Path(self.temporary_directory.name) / "onboarding.db"
        self.database = DatabaseService(path)
        await self.database.initialize()
        self.config = make_config(path)
        self.congressus_service = SimpleNamespace(
            create_authorization_url=AsyncMock(return_value="https://example.test/oauth"),
            set_verified_callback=Mock(),
        )
        congressus_cog = CongressusCog.__new__(CongressusCog)
        congressus_cog.service = self.congressus_service
        self.discord_user = SimpleNamespace(send=AsyncMock())
        self.bot = SimpleNamespace(
            config=self.config,
            database=self.database,
            get_cog=lambda name: congressus_cog if name == "CongressusCog" else None,
            get_user=lambda _user_id: self.discord_user,
            fetch_user=AsyncMock(return_value=self.discord_user),
            get_guild=lambda _guild_id: None,
        )
        self.cog = MinecraftCog(self.bot)

    async def asyncTearDown(self):
        self.temporary_directory.cleanup()

    def interaction(self, user_id=100, guild_id=6):
        return SimpleNamespace(
            user=SimpleNamespace(id=user_id),
            guild_id=guild_id,
            guild=SimpleNamespace(id=guild_id) if guild_id else None,
            response=CaptureResponse(),
            followup=CaptureFollowup(),
        )

    async def test_new_user_receives_congressus_verification(self):
        interaction = self.interaction()
        await self.cog.show_onboarding(interaction)

        self.congressus_service.create_authorization_url.assert_awaited_once_with(100)
        _, kwargs = interaction.response.sent
        self.assertIn("verifiëren", kwargs["embed"].description)
        self.assertEqual(kwargs["view"].children[0].label, "Verifiëren via Congressus")

    async def test_verified_user_skips_oauth_and_gets_link_button(self):
        await self.database.link_member(100, "congressus-100", "member", "Member")
        interaction = self.interaction()
        await self.cog.show_onboarding(interaction)

        self.congressus_service.create_authorization_url.assert_not_awaited()
        _, kwargs = interaction.response.sent
        self.assertIn("al geverifieerd", kwargs["embed"].description)
        self.assertIsInstance(kwargs["view"], MinecraftAccountLinkView)

    async def test_registered_user_sees_configured_server_address(self):
        await self.database.link_member(100, "congressus-100", "member", "Member")
        await self.database.register_minecraft_username(100, "PlayerOne")
        interaction = self.interaction()
        await self.cog.show_onboarding(interaction)

        _, kwargs = interaction.response.sent
        self.assertIn("PlayerOne", kwargs["embed"].description)
        self.assertIn(self.config.minecraft_server_host, kwargs["embed"].description)
        self.assertNotIn("view", kwargs)

    async def test_verified_user_can_open_username_modal(self):
        view = MinecraftAccountLinkView(self.cog, 100)
        interaction = self.interaction()

        self.assertTrue(await view.interaction_check(interaction))
        await view.link_account.callback(interaction)

        self.assertIsInstance(interaction.response.modal, MinecraftAccountModal)

    async def test_button_and_modal_are_bound_to_discord_user(self):
        view = MinecraftAccountLinkView(self.cog, 100)
        other_interaction = self.interaction(user_id=200)
        self.assertFalse(await view.interaction_check(other_interaction))

        modal = MinecraftAccountModal(self.cog, 100)
        await modal.on_submit(other_interaction)
        args, _ = other_interaction.response.sent
        self.assertIn("niet voor jouw", args[0])

    async def test_modal_uses_shared_registration_method_and_validation(self):
        self.cog.register_from_interaction = AsyncMock()
        modal = MinecraftAccountModal(self.cog, 100)
        modal.username._value = "PlayerOne"
        interaction = self.interaction()

        await modal.on_submit(interaction)

        self.cog.register_from_interaction.assert_awaited_once_with(
            interaction, "PlayerOne"
        )
        self.assertFalse(is_valid_minecraft_username("bad-name"))

    async def test_oauth_success_callback_is_connected_to_private_prompt(self):
        callback = self.congressus_service.set_verified_callback.call_args.args[0]
        await callback(100)

        self.discord_user.send.assert_awaited_once()
        kwargs = self.discord_user.send.await_args.kwargs
        self.assertIn("Verificatie geslaagd", kwargs["embed"].description)
        self.assertIsInstance(kwargs["view"], MinecraftAccountLinkView)

    def test_status_and_players_are_dutch_and_not_raw_json(self):
        status = _format_status(
            {"status": "healthy", "running": True},
            self.config.minecraft_server_host,
        )
        players = _format_players(
            {"output": "There are 2 of a max of 30 players online: Alice, Bob"}
        )

        self.assertIn("Server online", status)
        self.assertIn("Status: gezond", status)
        self.assertIn(self.config.minecraft_server_host, status)
        self.assertNotIn("{", status)
        self.assertIn("Spelers online: 2 / 30", players)
        self.assertIn("Alice", players)
        self.assertNotIn("There are", players)


class MinecraftManagementWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        path = Path(self.temporary_directory.name) / "realtime.db"
        self.database = DatabaseService(path)
        await self.database.initialize()
        self.config = make_config(path)
        await self.database.link_member(100, "congressus-100", "member", "Member")
        self.api = SimpleNamespace(
            start=AsyncMock(),
            close=AsyncMock(),
            whitelist_add=AsyncMock(),
            whitelist_remove=AsyncMock(),
        )
        self.service = MinecraftService(self.config, self.database, self.api)

    async def asyncTearDown(self):
        self.temporary_directory.cleanup()

    async def test_failed_register_sync_keeps_database_state(self):
        self.api.whitelist_add.side_effect = MinecraftApiUnavailable("offline")
        with self.assertRaises(RegistrationSyncFailed):
            await self.service.register(100, "PlayerOne")
        member = await self.database.get_member(100)
        self.assertEqual(member.minecraft_username, "PlayerOne")

    async def test_reset_removes_server_entry_before_database(self):
        await self.database.register_minecraft_username(100, "PlayerOne")

        async def assert_database_still_has_username(_username):
            member = await self.database.get_member(100)
            self.assertEqual(member.minecraft_username, "PlayerOne")

        self.api.whitelist_remove.side_effect = assert_database_still_has_username
        removed = await self.service.reset(100)
        self.assertEqual(removed, "PlayerOne")
        self.assertIsNone((await self.database.get_member(100)).minecraft_username)

    async def test_failed_reset_removal_leaves_database_unchanged(self):
        await self.database.register_minecraft_username(100, "PlayerOne")
        self.api.whitelist_remove.side_effect = MinecraftApiUnavailable("offline")
        with self.assertRaises(MinecraftApiUnavailable):
            await self.service.reset(100)
        self.assertEqual(
            (await self.database.get_member(100)).minecraft_username, "PlayerOne"
        )

    async def test_set_replaces_old_username_in_safe_order(self):
        await self.database.register_minecraft_username(100, "OldName")
        actions = []
        self.api.whitelist_remove.side_effect = lambda username: actions.append(
            ("remove", username)
        )
        self.api.whitelist_add.side_effect = lambda username: actions.append(("add", username))

        member = await self.service.set_username(100, "NewName")

        self.assertEqual(actions, [("remove", "OldName"), ("add", "NewName")])
        self.assertEqual(member.minecraft_username, "NewName")

    async def test_set_rolls_back_old_entry_when_new_add_fails(self):
        await self.database.register_minecraft_username(100, "OldName")
        actions = []

        async def remove(username):
            actions.append(("remove", username))

        async def add(username):
            actions.append(("add", username))
            if username == "NewName":
                raise MinecraftApiUnavailable("offline")

        self.api.whitelist_remove.side_effect = remove
        self.api.whitelist_add.side_effect = add

        with self.assertRaises(WhitelistChangeFailed) as raised:
            await self.service.set_username(100, "NewName")

        self.assertFalse(raised.exception.rollback_failed)
        self.assertEqual(
            actions,
            [("remove", "OldName"), ("add", "NewName"), ("add", "OldName")],
        )
        self.assertEqual((await self.database.get_member(100)).minecraft_username, "OldName")

    async def test_sync_does_not_change_database(self):
        await self.database.register_minecraft_username(100, "PlayerOne")
        before = await self.database.get_member(100)
        username = await self.service.sync(100)
        after = await self.database.get_member(100)

        self.assertEqual(username, "PlayerOne")
        self.assertEqual(before, after)
        self.api.whitelist_add.assert_awaited_once_with("PlayerOne")


def make_member(*role_ids, administrator=False):
    member = Mock(spec=discord.Member)
    member.roles = [SimpleNamespace(id=role_id) for role_id in role_ids]
    member.guild_permissions = SimpleNamespace(administrator=administrator)
    return member


class PermissionAndOfficeTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_and_lifecycle_permissions(self):
        config = make_config(Path("unused.db"))
        cog = MinecraftCog.__new__(MinecraftCog)
        root = cog.__cog_app_commands_group__
        start = next(command for command in root.commands if command.name == "start")
        whitelist = next(command for command in root.commands if command.name == "whitelist")
        sync = next(command for command in whitelist.commands if command.name == "sync")

        for command in (start, sync):
            predicate = command.checks[0]
            for interaction in (
                SimpleNamespace(
                    user=make_member(administrator=True), client=SimpleNamespace(config=config)
                ),
                SimpleNamespace(
                    user=make_member(config.board_role_id),
                    client=SimpleNamespace(config=config),
                ),
                SimpleNamespace(
                    user=make_member(config.minecraft_manager_role_id),
                    client=SimpleNamespace(config=config),
                ),
            ):
                self.assertTrue(await predicate(interaction))

            with self.assertRaises(ManagementPermissionError):
                await predicate(
                    SimpleNamespace(
                        user=make_member(config.minecraft_role_id),
                        client=SimpleNamespace(config=config),
                    )
                )

    async def test_office_conditional_rename_is_unchanged(self):
        config = make_config(Path("unused.db"))
        channel = Mock(spec=discord.VoiceChannel)
        channel.name = CLOSED_CHANNEL_NAME
        channel.edit = AsyncMock()
        interaction = SimpleNamespace(
            user=make_member(config.board_role_id),
            client=SimpleNamespace(get_channel=lambda _channel_id: channel),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await OfficeButtons(config)._set_office_state(
            interaction, CLOSED_CHANNEL_NAME, "closed"
        )

        channel.edit.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
