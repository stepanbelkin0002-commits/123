"""Offline contract tests. FakeClient never opens a network connection."""

from dataclasses import replace
from types import SimpleNamespace
import unittest

from fastapi.testclient import TestClient
from telethon import errors
from telethon.crypto import AuthKey
from telethon.sessions import StringSession

from main import Settings, create_app, decrypt_session, encrypt_session


ORIGIN = "https://testserver"
PANEL_PASSWORD = "test-only-owner-password-52-characters-long"
SETTINGS = Settings(api_id="12345", api_hash="b" * 32,
                    dashboard_password=PANEL_PASSWORD, origin=ORIGIN)


def fake_session():
    session = StringSession()
    session.set_dc(2, "192.0.2.1", 443)
    session.auth_key = AuthKey(b"a" * 256)
    return session


class FakeClient:
    instances = []
    flood_on_send = False
    require_password = True

    def __init__(self, session, *args, **kwargs):
        self.session = session
        self.connected = False
        self.authorized = bool(session.auth_key)
        self.sent_codes = 0
        self.sign_in_calls = 0
        self.instances.append(self)

    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def is_user_authorized(self):
        return self.authorized

    async def send_code_request(self, phone):
        self.sent_codes += 1
        if self.flood_on_send:
            raise errors.FloodWaitError(None, capture=120)
        return SimpleNamespace(phone_code_hash="fake-phone-code-hash")

    async def sign_in(self, phone=None, code=None, phone_code_hash=None, password=None):
        self.sign_in_calls += 1
        if password is not None:
            if password != "fake-test-only-password":
                raise errors.PasswordHashInvalidError(None)
        else:
            if code != "54321":
                raise errors.PhoneCodeInvalidError(None)
            if self.require_password:
                raise errors.SessionPasswordNeededError(None)
        self.session = fake_session()
        self.authorized = True
        return await self.get_me()

    async def get_me(self):
        return SimpleNamespace(id=123456, first_name="Test Account", username="test_account")


class LoginTests(unittest.TestCase):
    def setUp(self):
        FakeClient.instances = []
        FakeClient.flood_on_send = False
        FakeClient.require_password = True
        self.context = TestClient(create_app(SETTINGS, FakeClient), base_url=ORIGIN)
        self.client = self.context.__enter__()
        self.headers = {"Origin": ORIGIN}

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def unlock(self):
        response = self.client.post("/api/unlock", json={"password": PANEL_PASSWORD}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.headers["X-CSRF-Token"] = response.json()["csrf"]
        return response

    def post(self, path, data):
        return self.client.post(path, json=data, headers=self.headers)

    def complete_login(self):
        self.assertEqual(self.post("/api/login/start", {"phone": "+19995550123"}).status_code, 200)
        response = self.post("/api/login/code", {"code": "54321"})
        self.assertEqual(response.json()["state"], "password_needed")
        self.assertEqual(self.post("/api/login/password", {"password": "fake-test-only-password"}).status_code, 200)

    def test_account_endpoints_require_dashboard_authentication(self):
        self.assertEqual(self.client.get("/api/status").status_code, 401)
        self.assertEqual(self.post("/api/login/start", {"phone": "+19995550123"}).status_code, 401)
        self.assertEqual(self.post("/api/session/export", {}).status_code, 401)
        self.assertEqual(FakeClient.instances, [])

    def test_cookie_security_and_cross_site_requests(self):
        self.assertEqual(self.client.post("/api/unlock", json={"password": PANEL_PASSWORD}).status_code, 403)
        response = self.client.post("/api/unlock", json={"password": PANEL_PASSWORD},
                                    headers={"Origin": "https://attacker.invalid"})
        self.assertEqual(response.status_code, 403)
        cookie = self.unlock().headers["set-cookie"]
        for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/"):
            self.assertIn(flag, cookie)
        response = self.client.post("/api/login/start", json={"phone": "+19995550123"},
                                    headers={"Origin": ORIGIN})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(FakeClient.instances, [])

    def test_complete_2fa_flow_and_encrypted_session_export(self):
        self.unlock()
        self.assertEqual(self.post("/api/login/start", {"phone": "+1 (999) 555-0123"}).status_code, 200)
        self.assertEqual(self.post("/api/login/code", {"code": "00000"}).status_code, 422)
        self.assertEqual(self.post("/api/login/code", {"code": "54321"}).json()["state"], "password_needed")
        self.assertEqual(self.post("/api/login/password", {"password": "wrong-password"}).status_code, 422)
        self.assertEqual(self.post("/api/login/password", {"password": "fake-test-only-password"}).status_code, 200)
        state = self.client.get("/api/status").json()
        self.assertTrue(state["authorized"])
        self.assertFalse(state["saved"])
        self.assertEqual(state["account"]["id"], 123456)
        response = self.post("/api/session/export", {})
        self.assertEqual(response.status_code, 200)
        encrypted = response.json()["encrypted_session"]
        expected = FakeClient.instances[0].session.save()
        self.assertNotIn(expected, response.text)
        self.assertEqual(decrypt_session(encrypted, PANEL_PASSWORD), expected)

    def test_saved_session_restores_without_requesting_a_code(self):
        encrypted = encrypt_session(fake_session().save(), PANEL_PASSWORD)
        settings = replace(SETTINGS, encrypted_session=encrypted)
        with TestClient(create_app(settings, FakeClient), base_url=ORIGIN) as client:
            csrf = client.post("/api/unlock", json={"password": PANEL_PASSWORD},
                               headers={"Origin": ORIGIN}).json()["csrf"]
            state = client.get("/api/status").json()
            self.assertTrue(state["authorized"])
            self.assertTrue(state["saved"])
            response = client.post("/api/login/start", json={"phone": "+19995550123"},
                                   headers={"Origin": ORIGIN, "X-CSRF-Token": csrf})
            self.assertEqual(response.json()["state"], "authorized")
            self.assertEqual(FakeClient.instances[-1].sent_codes, 0)

    def test_broken_session_fails_closed_instead_of_replacing_it(self):
        with TestClient(create_app(replace(SETTINGS, encrypted_session="broken"), FakeClient), base_url=ORIGIN) as client:
            client.post("/api/unlock", json={"password": PANEL_PASSWORD}, headers={"Origin": ORIGIN})
            self.assertEqual(client.get("/api/status").status_code, 503)
            self.assertEqual(FakeClient.instances, [])

    def test_repeated_code_requests_are_limited(self):
        self.unlock()
        self.assertEqual(self.post("/api/login/start", {"phone": "+19995550123"}).status_code, 200)
        self.assertEqual(self.post("/api/login/start", {"phone": "+19995550123"}).status_code, 429)
        self.assertEqual(FakeClient.instances[0].sent_codes, 1)

    def test_expired_2fa_returns_to_the_phone_step(self):
        self.unlock()
        self.post("/api/login/start", {"phone": "+19995550123"})
        self.post("/api/login/code", {"code": "54321"})
        self.client.app.state.login_flow.pending_until = 1.0
        state = self.client.get("/api/status").json()
        self.assertFalse(state["needs_password"])
        self.assertFalse(state["pending_code"])
        self.assertEqual(self.post("/api/login/password", {"password": "fake-test-only-password"}).status_code, 409)

    def test_expired_code_does_not_reach_telegram(self):
        self.unlock()
        self.post("/api/login/start", {"phone": "+19995550123"})
        self.client.app.state.login_flow.pending_until = 1.0
        self.assertEqual(self.post("/api/login/code", {"code": "54321"}).status_code, 409)
        self.assertEqual(FakeClient.instances[0].sign_in_calls, 0)

    def test_telegram_flood_wait_stops_further_calls(self):
        FakeClient.flood_on_send = True
        self.unlock()
        response = self.post("/api/login/start", {"phone": "+19995550123"})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["retry-after"], "120")
        self.assertEqual(self.post("/api/login/start", {"phone": "+19995550123"}).status_code, 429)
        self.assertEqual(FakeClient.instances[0].sent_codes, 1)

    def test_validation_does_not_echo_secrets(self):
        self.unlock()
        fake_secret = "private-test-content" * 50
        response = self.post("/api/login/password", {"password": fake_secret})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(fake_secret, response.text)
        self.assertNotIn("input", response.text)

    def test_five_bad_passwords_stop_local_retries(self):
        self.unlock()
        self.post("/api/login/start", {"phone": "+19995550123"})
        self.post("/api/login/code", {"code": "54321"})
        for _ in range(5):
            self.assertEqual(self.post("/api/login/password", {"password": "wrong"}).status_code, 422)
        self.assertEqual(self.post("/api/login/password", {"password": "wrong"}).status_code, 429)

    def test_lock_rejects_the_previous_dashboard_cookie(self):
        self.unlock()
        self.assertEqual(self.post("/api/lock", {}).status_code, 200)
        self.assertEqual(self.client.get("/api/status").status_code, 401)

    def test_static_page_and_health_do_not_contact_telegram(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        for path in ("/health", "/assets/app.js", "/assets/style.css"):
            self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(FakeClient.instances, [])

    def test_wrong_panel_password_is_rate_limited(self):
        for _ in range(10):
            self.assertEqual(self.post("/api/unlock", {"password": "wrong"}).status_code, 401)
        self.assertEqual(self.post("/api/unlock", {"password": "wrong"}).status_code, 429)


class SessionCryptoTests(unittest.TestCase):
    def test_wrong_password_and_modified_session_are_rejected(self):
        session = fake_session().save()
        encrypted = encrypt_session(session, PANEL_PASSWORD)
        self.assertEqual(decrypt_session(encrypted, PANEL_PASSWORD), session)
        with self.assertRaises(ValueError):
            decrypt_session(encrypted, "different-password")
        parts = encrypted.split(".")
        cipher = bytearray(__import__("base64").urlsafe_b64decode(parts[-1]))
        cipher[0] ^= 1
        parts[-1] = __import__("base64").urlsafe_b64encode(cipher).decode()
        with self.assertRaises(ValueError):
            decrypt_session(".".join(parts), PANEL_PASSWORD)


if __name__ == "__main__":
    unittest.main(verbosity=2)
