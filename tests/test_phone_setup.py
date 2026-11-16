import io
from contextlib import redirect_stdout
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from phone import setup as phone_setup
from telethon import types


class FakePhoneClient:
    def __init__(self, *args, **kwargs):
        self.session = SimpleNamespace(save_entities=True)
        self.connected = False
        self.target = types.Channel(id=phone_setup.CHANNEL_ID, title="Test city channel",
                                    photo=types.ChatPhotoEmpty(), date=None,
                                    creator=True, broadcast=True, username=phone_setup.CHANNEL_USERNAME)

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def is_user_authorized(self):
        return True

    async def get_entity(self, username):
        if username != phone_setup.CHANNEL_USERNAME:
            raise AssertionError("Only the configured public channel may be read")
        return self.target


class PhoneSetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_onboarding_checks_channel_without_publishing_or_enabling_automation(self):
        client, saved = FakePhoneClient(), []
        settings = dict(api_id="12345", api_hash="b" * 32)
        output = io.StringIO()
        with patch.object(phone_setup, "load_settings", return_value=settings), \
                patch.object(phone_setup, "save_settings", side_effect=saved.append), \
                patch.object(phone_setup, "TelegramClient", return_value=client), redirect_stdout(output):
            result = await phone_setup.setup()
        self.assertEqual(result, 0)
        self.assertFalse(client.connected)
        self.assertFalse(client.session.save_entities)
        self.assertEqual(saved[0]["channel_id"], phone_setup.CHANNEL_ID)
        self.assertFalse(saved[0]["automation_enabled"])
        self.assertNotIn(settings["api_hash"], output.getvalue())

    async def test_onboarding_does_not_confirm_another_accounts_channel(self):
        client, saved = FakePhoneClient(), []
        client.target.creator = False
        with patch.object(phone_setup, "load_settings", return_value=dict(api_id="12345", api_hash="b" * 32)), \
                patch.object(phone_setup, "save_settings", side_effect=saved.append), \
                patch.object(phone_setup, "TelegramClient", return_value=client), redirect_stdout(io.StringIO()):
            result = await phone_setup.setup()
        self.assertEqual(result, 1)
        self.assertEqual(saved, [])
        self.assertFalse(client.connected)


if __name__ == "__main__":
    unittest.main()
