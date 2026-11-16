import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import unittest

from telethon import errors, functions, types
from telethon.crypto import AuthKey
from telethon.sessions import StringSession

from channel_setup import (ChannelJob, SetupStopped, archive_other_dialogs,
                           ensure_channel, leave_unavailable_channels, run_configured_setup)
from main import Settings


NOW = datetime(2026, 11, 16, 21, 0, tzinfo=timezone.utc)
TITLE = "Чё там, Чебоксары?"
ABOUT = "Городские новости Чебоксар, транспорт и события. С датами и ссылками на источники."


def job_data(**changes):
    data = {"action": "create_channel", "job_id": "a" * 32, "title": TITLE,
            "about": ABOUT, "username_candidates": ["che_tam_cheboksary", "chetam_cheb21"],
            "created_after": (NOW - timedelta(minutes=5)).isoformat(),
            "expires_at": (NOW + timedelta(minutes=20)).isoformat(), "allow_create": True}
    data.update(changes)
    return data


def job(**changes):
    return ChannelJob.model_validate_json(json.dumps(job_data(**changes)))


def channel(channel_id=1001, *, title=TITLE, creator=True, date=NOW, username=None):
    return types.Channel(id=channel_id, title=title, photo=types.ChatPhotoEmpty(),
                         date=date, creator=creator, broadcast=True,
                         username=username, access_hash=123456)


def test_session():
    value = StringSession()
    value.set_dc(2, "192.0.2.1", 443)
    value.auth_key = AuthKey(b"x" * 256)
    return value.save()


class FakeChannelClient:
    def __init__(self, *args, **kwargs):
        self.channels = []
        self.descriptions = {}
        self.created = 0
        self.username_checks = []
        self.username_updates = []
        self.unavailable = set()
        self.public_limit = False
        self.flood_on_username = False
        self.connected = False
        self.authorized = True
        self.options = kwargs
        self.archived_ids = set()
        self.folder_edits = []
        self.left_ids = []
        self.privacy = types.GlobalPrivacySettings(
            archive_and_mute_new_noncontact_peers=False, keep_archived_unmuted=False,
            keep_archived_folders=False, hide_read_marks=True,
            new_noncontact_peers_require_premium=False, display_gifts_button=True,
            noncontact_peers_paid_stars=3)

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def is_user_authorized(self):
        return self.authorized

    async def iter_dialogs(self, **kwargs):
        items = self.channels[:kwargs["limit"]]
        for item in items:
            if kwargs.get("ignore_migrated") and isinstance(item, types.Chat) and item.migrated_to:
                continue
            if kwargs.get("folder") == 0 and __import__("telethon").utils.get_peer_id(item) in self.archived_ids:
                continue
            yield SimpleNamespace(entity=item)

    async def edit_folder(self, peers, folder):
        if folder != 1:
            raise AssertionError("Cleanup must only archive")
        if not isinstance(peers, list):
            peers = [peers]
        for item in peers:
            peer_id = __import__("telethon").utils.get_peer_id(item)
            self.archived_ids.add(peer_id)
            self.folder_edits.append(peer_id)

    async def __call__(self, request):
        if isinstance(request, functions.channels.LeaveChannelRequest):
            target_id = request.channel.channel_id
            target = next(item for item in self.channels if item.id == target_id)
            if not isinstance(target, types.ChannelForbidden):
                raise AssertionError("Must not leave accessible private channels")
            self.channels.remove(target)
            self.left_ids.append(target_id)
            return SimpleNamespace()
        if isinstance(request, functions.account.GetGlobalPrivacySettingsRequest):
            return self.privacy
        if isinstance(request, functions.account.SetGlobalPrivacySettingsRequest):
            self.privacy = request.settings
            return self.privacy
        if isinstance(request, functions.channels.GetAdminedPublicChannelsRequest):
            if self.public_limit:
                raise errors.ChannelsAdminPublicTooMuchError(None)
            return SimpleNamespace(chats=[])
        if isinstance(request, functions.channels.CreateChannelRequest):
            self.created += 1
            item = channel(1000 + self.created, title=request.title)
            self.channels.insert(0, item)
            self.descriptions[item.id] = request.about
            return SimpleNamespace(chats=[item])
        if isinstance(request, functions.channels.GetFullChannelRequest):
            item = next(item for item in self.channels if item.id == request.channel.id)
            return SimpleNamespace(chats=[item], full_chat=SimpleNamespace(about=self.descriptions[item.id]))
        if isinstance(request, functions.channels.CheckUsernameRequest):
            self.username_checks.append(request.username)
            if self.flood_on_username:
                raise errors.FloodWaitError(None, capture=180)
            return request.username not in self.unavailable
        if isinstance(request, functions.channels.UpdateUsernameRequest):
            item = next(item for item in self.channels if item.id == request.channel.id)
            item.username = request.username
            self.username_updates.append(request.username)
            return True
        raise AssertionError("Unexpected Telegram operation")


class FakeFlow:
    def __init__(self, encrypted="test-only-encrypted-session"):
        self.settings = Settings(api_id="12345", api_hash="b" * 32,
                                 dashboard_password="test-panel-password-more-than-24-characters",
                                 encrypted_session=encrypted, origin="https://testserver")
        self.lock = asyncio.Lock()
        self.client = None
        self.authorized = False

    async def close(self):
        pass


async def no_sleep(_seconds):
    pass


class ChannelSetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_creation_checks_username_and_verifies_owned_broadcast(self):
        client = FakeChannelClient()
        client.unavailable.add("che_tam_cheboksary")
        result = await ensure_channel(client, job(), emit=lambda _: None, now=lambda: NOW)
        self.assertEqual(client.created, 1)
        self.assertEqual(client.username_updates, ["chetam_cheb21"])
        self.assertEqual(result["url"], "https://t.me/chetam_cheb21")
        self.assertTrue(result["verified"])
        self.assertTrue(result["public"])

    async def test_second_execution_reuses_the_channel(self):
        client = FakeChannelClient()
        first = await ensure_channel(client, job(), emit=lambda _: None, now=lambda: NOW)
        second = await ensure_channel(client, job(), emit=lambda _: None, now=lambda: NOW)
        self.assertEqual(client.created, 1)
        self.assertEqual(first["channel_id"], second["channel_id"])
        self.assertFalse(second["created"])
        self.assertEqual(len(client.username_updates), 1)

    async def test_recovery_searches_beyond_two_hundred_dialogs(self):
        client = FakeChannelClient()
        managed = channel(1001, username="che_tam_cheboksary")
        client.channels = [types.User(id=10000 + index, first_name="Other", access_hash=123)
                           for index in range(205)] + [managed]
        client.descriptions[managed.id] = ABOUT
        result = await ensure_channel(client, job(), emit=lambda _: None, now=lambda: NOW)
        self.assertEqual(client.created, 0)
        self.assertEqual(result["channel_id"], managed.id)

    async def test_existing_channel_is_recovered_outside_creation_time_window(self):
        client = FakeChannelClient()
        managed = channel(date=NOW - timedelta(days=2), username="che_tam_cheboksary")
        client.channels = [managed]
        client.descriptions[managed.id] = ABOUT
        result = await ensure_channel(client, job(allow_create=False, existing_channel_id=managed.id),
                                      emit=lambda _: None, now=lambda: NOW)
        self.assertFalse(result["created"])

    async def test_recovery_reuses_private_channel_after_partial_creation(self):
        client = FakeChannelClient()
        item = channel()
        client.channels = [item]
        client.descriptions[item.id] = ABOUT
        result = await ensure_channel(client, job(allow_create=False), emit=lambda _: None, now=lambda: NOW)
        self.assertEqual(client.created, 0)
        self.assertTrue(result["public"])

    async def test_old_or_other_owned_channels_are_not_changed(self):
        client = FakeChannelClient()
        old = channel(500, date=NOW - timedelta(days=1))
        other = channel(501, creator=False)
        client.channels = [old, other]
        client.descriptions = {500: ABOUT, 501: ABOUT}
        result = await ensure_channel(client, job(), emit=lambda _: None, now=lambda: NOW)
        self.assertTrue(result["created"])
        self.assertIsNone(old.username)
        self.assertIsNone(other.username)

    async def test_ambiguous_recovery_never_creates_another_channel(self):
        client = FakeChannelClient()
        client.channels = [channel(501), channel(502)]
        client.descriptions = {501: ABOUT, 502: ABOUT}
        with self.assertRaisesRegex(SetupStopped, "MULTIPLE_MATCHING_CHANNELS"):
            await ensure_channel(client, job(), emit=lambda _: None, now=lambda: NOW)
        self.assertEqual(client.created, 0)

    async def test_public_channel_limit_stops_before_creation(self):
        client = FakeChannelClient()
        client.public_limit = True
        with self.assertRaises(errors.ChannelsAdminPublicTooMuchError):
            await ensure_channel(client, job(), emit=lambda _: None, now=lambda: NOW)
        self.assertEqual(client.created, 0)

    async def test_flood_wait_is_not_automatically_retried(self):
        client = FakeChannelClient()
        client.flood_on_username = True
        with self.assertRaises(errors.FloodWaitError):
            await ensure_channel(client, job(), emit=lambda _: None, now=lambda: NOW)
        self.assertEqual(client.created, 1)
        self.assertEqual(len(client.username_checks), 1)

    async def test_expired_job_has_no_side_effects(self):
        client = FakeChannelClient()
        with self.assertRaisesRegex(SetupStopped, "JOB_EXPIRED"):
            await ensure_channel(client, job(), emit=lambda _: None,
                                 now=lambda: NOW + timedelta(hours=1))
        self.assertEqual(client.created, 0)

    async def test_recovery_only_never_creates_if_not_found(self):
        client = FakeChannelClient()
        with self.assertRaisesRegex(SetupStopped, "RECOVERY_NOT_FOUND"):
            await ensure_channel(client, job(allow_create=False), emit=lambda _: None, now=lambda: NOW)
        self.assertEqual(client.created, 0)

    async def test_archive_preserves_the_new_channel_and_existing_memberships(self):
        client = FakeChannelClient()
        managed, old_channel = channel(1001), channel(1002, title="Other channel")
        bot = types.User(id=5001, first_name="Example bot", bot=True, access_hash=123)
        person = types.User(id=5002, first_name="Example person", access_hash=123)
        group = types.Chat(id=5003, title="Example group", photo=types.ChatPhotoEmpty(),
                           participants_count=2, date=NOW, version=1)
        client.channels = [managed, old_channel, bot, person, group]
        events = []
        result = await archive_other_dialogs(client, job(archive_existing_dialogs=True),
                                             1001, emit=events.append, now=lambda: NOW)
        self.assertTrue(result["verified"])
        self.assertEqual(result["archived"], 4)
        self.assertEqual(len(client.channels), 5)
        self.assertNotIn(__import__("telethon").utils.get_peer_id(managed), client.archived_ids)
        self.assertNotIn("Example", json.dumps(events))

    async def test_archive_repeated_execution_is_idempotent(self):
        client = FakeChannelClient()
        client.channels = [channel(1001), channel(1002, title="Other channel")]
        await archive_other_dialogs(client, job(), 1001, emit=lambda _: None, now=lambda: NOW)
        second = await archive_other_dialogs(client, job(), 1001, emit=lambda _: None, now=lambda: NOW)
        self.assertEqual(second["archived"], 0)
        self.assertTrue(second["verified"])

    async def test_migrated_group_placeholders_are_not_main_list_dialogs(self):
        client = FakeChannelClient()
        ghost = types.Chat(id=888, title="Migrated placeholder", photo=types.ChatPhotoEmpty(),
                           participants_count=1, date=NOW, version=1,
                           migrated_to=types.InputChannel(889, 123))
        client.channels = [channel(1001), ghost]
        result = await archive_other_dialogs(client, job(), 1001, emit=lambda _: None, now=lambda: NOW)
        self.assertTrue(result["verified"])
        self.assertEqual(result["archived"], 0)

    async def test_only_inaccessible_channels_are_left(self):
        client = FakeChannelClient()
        managed = channel(1001)
        accessible_private = channel(1002, title="Accessible private channel", creator=False)
        forbidden = types.ChannelForbidden(id=1003, access_hash=123, title="Unavailable", broadcast=True)
        person = types.User(id=5004, first_name="Personal contact", access_hash=123)
        client.channels = [managed, accessible_private, forbidden, person]
        result = await leave_unavailable_channels(client, job(), 1001,
                                                  emit=lambda _: None, now=lambda: NOW)
        self.assertTrue(result["verified"])
        self.assertEqual(client.left_ids, [1003])
        self.assertIn(accessible_private, client.channels)
        self.assertIn(person, client.channels)

    async def test_combined_setup_leaves_forbidden_and_archives_personal_dialog(self):
        flow, events, clients = FakeFlow(), [], []

        def factory(*args, **kwargs):
            client = FakeChannelClient(*args, **kwargs)
            client.channels = [types.ChannelForbidden(id=3001, access_hash=123, title="Unavailable", broadcast=True),
                               types.User(id=3002, first_name="Personal contact", access_hash=123)]
            clients.append(client)
            return client

        await run_configured_setup(json.dumps(job_data(archive_existing_dialogs=True, leave_unavailable_channels=True)),
                                   flow, decrypt=lambda *_: test_session(), client_factory=factory,
                                   sleep=no_sleep, emit=events.append, now=lambda: NOW)
        result = events[-1]
        self.assertEqual(result["phase"], "done")
        self.assertTrue(result["archive"]["verified"])
        self.assertEqual(result["archive"]["personal_dialogs_archived"], 1)
        self.assertEqual(result["unavailable_channels"]["left"], 1)
        self.assertNotIn("Personal contact", json.dumps(events))

    async def test_keep_archive_changes_only_the_requested_archive_flag(self):
        client = FakeChannelClient()
        client.channels = [channel(1001)]
        before = client.privacy.to_dict()
        result = await archive_other_dialogs(client, job(), 1001, emit=lambda _: None, now=lambda: NOW)
        after = client.privacy.to_dict()
        self.assertTrue(result["retention_enabled"])
        self.assertEqual({key: value for key, value in before.items() if key != "keep_archived_unmuted"},
                         {key: value for key, value in after.items() if key != "keep_archived_unmuted"})

    async def test_runner_requires_saved_session(self):
        events = []
        await run_configured_setup(json.dumps(job_data()), FakeFlow(encrypted=""),
                                   decrypt=lambda *_: self.fail("Decryption must not run"),
                                   client_factory=lambda *_args, **_kwargs: self.fail("No client expected"),
                                   sleep=no_sleep, emit=events.append, now=lambda: NOW)
        self.assertEqual(events[-1]["reason"], "SAVED_SESSION_REQUIRED")

    async def test_runner_keeps_credentials_out_of_results(self):
        events, clients = [], []
        flow = FakeFlow()

        def factory(*args, **kwargs):
            client = FakeChannelClient(*args, **kwargs)
            clients.append(client)
            return client

        await run_configured_setup(json.dumps(job_data()), flow,
                                   decrypt=lambda *_: test_session(), client_factory=factory,
                                   sleep=no_sleep, emit=events.append, now=lambda: NOW)
        self.assertEqual(events[-1]["phase"], "done")
        self.assertFalse(clients[0].connected)
        self.assertEqual(clients[0].options["request_retries"], 0)
        serialized = json.dumps(events)
        for secret in (flow.settings.api_hash, flow.settings.dashboard_password,
                       flow.settings.encrypted_session, test_session()):
            self.assertNotIn(secret, serialized)

    async def test_decryption_error_does_not_print_exception_text(self):
        events = []
        private_text = "private-test-password-never-print"

        def failing_decrypt(*_):
            raise ValueError(private_text)

        await run_configured_setup(json.dumps(job_data()), FakeFlow(), decrypt=failing_decrypt,
                                   sleep=no_sleep, emit=events.append, now=lambda: NOW)
        self.assertEqual(events[-1]["reason"], "ValueError")
        self.assertNotIn(private_text, json.dumps(events))

    async def test_runner_propagates_shutdown_cancellation(self):
        async def cancel(_):
            raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await run_configured_setup(json.dumps(job_data()), FakeFlow(),
                                       decrypt=lambda *_: test_session(),
                                       sleep=cancel, emit=lambda _: None, now=lambda: NOW)


if __name__ == "__main__":
    unittest.main(verbosity=2)
