"""An owner-operated Telegram login helper for a single Render Free instance.

No credentials, login codes, passwords or session keys are written to files.
Account access is restored from an encrypted secret set by the owner on Render.
"""

import asyncio
import base64
from collections import deque
from contextlib import asynccontextmanager
from contextlib import suppress
from dataclasses import dataclass
import hashlib
import hmac
import logging
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from telethon import TelegramClient, errors
from telethon.sessions import StringSession


ROOT = Path(__file__).resolve().parent
COOKIE = "__Host-tg-dashboard"
AAD = b"personal-telegram-session-v1"


@dataclass(frozen=True)
class Settings:
    api_id: str = ""
    api_hash: str = ""
    dashboard_password: str = ""
    encrypted_session: str = ""
    origin: str = "https://localhost"

    @classmethod
    def from_env(cls):
        return cls(
            api_id=os.getenv("TG_API_ID", "").strip(),
            api_hash=os.getenv("TG_API_HASH", "").strip(),
            dashboard_password=os.getenv("DASHBOARD_PASSWORD", ""),
            encrypted_session=os.getenv("TG_SESSION_ENCRYPTED", "").strip(),
            origin=(os.getenv("APP_ORIGIN") or os.getenv("RENDER_EXTERNAL_URL")
                    or "https://localhost").rstrip("/"),
        )

    @property
    def api_ready(self):
        return (self.api_id.isascii() and self.api_id.isdigit()
                and int(self.api_id) > 0
                and re.fullmatch(r"[a-fA-F0-9]{32}", self.api_hash) is not None)

    @property
    def password_ready(self):
        return len(self.dashboard_password) >= 24


def _b64(data):
    return base64.urlsafe_b64encode(data).decode("ascii")


def _unb64(data):
    return base64.b64decode(data, altchars=b"-_", validate=True)


def _key(password, salt):
    return Scrypt(salt=salt, length=32, n=2**14, r=8, p=1).derive(password.encode("utf-8"))


def encrypt_session(session, password):
    salt, nonce = os.urandom(16), os.urandom(12)
    cipher = AESGCM(_key(password, salt)).encrypt(nonce, session.encode("utf-8"), AAD)
    return ".".join(("v1", _b64(salt), _b64(nonce), _b64(cipher)))


def decrypt_session(value, password):
    if not value or len(value) > 4096:
        raise ValueError("Invalid encrypted session")
    try:
        version, raw_salt, raw_nonce, raw_cipher = value.split(".")
        salt, nonce, cipher = map(_unb64, (raw_salt, raw_nonce, raw_cipher))
        if version != "v1" or len(salt) != 16 or len(nonce) != 12:
            raise ValueError("Invalid encrypted session")
        return AESGCM(_key(password, salt)).decrypt(nonce, cipher, AAD).decode("utf-8")
    except (ValueError, InvalidTag, UnicodeError) as exc:
        raise ValueError("Invalid encrypted session") from exc


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Unlock(StrictModel):
    password: str = Field(min_length=1, max_length=512)


class Phone(StrictModel):
    phone: str = Field(min_length=7, max_length=32)


class Code(StrictModel):
    code: str = Field(min_length=3, max_length=12)


class Password(StrictModel):
    password: str = Field(min_length=1, max_length=256)


class LoginFlow:
    def __init__(self, settings, client_factory=TelegramClient):
        self.settings = settings
        self.client_factory = client_factory
        self.client = None
        self.lock = asyncio.Lock()
        self.phone = None
        self.phone_code_hash = None
        self.pending_until = 0.0
        self.last_send = -60.0
        self.blocked_until = 0.0
        self.code_attempts = 0
        self.password_attempts = 0
        self.needs_password = False
        self.authorized = False

    def check_wait(self):
        if self.blocked_until > time.monotonic():
            seconds = max(1, int(self.blocked_until - time.monotonic()) + 1)
            raise HTTPException(429, f"Telegram просит подождать {seconds} сек.",
                                headers={"Retry-After": str(seconds)})

    async def ready(self):
        self.check_wait()
        if not self.settings.api_ready:
            raise HTTPException(503, "Добавь TG_API_ID и TG_API_HASH в Environment на Render.")
        if self.client is None:
            session = ""
            if self.settings.encrypted_session:
                try:
                    session = decrypt_session(self.settings.encrypted_session,
                                              self.settings.dashboard_password)
                except ValueError:
                    raise HTTPException(503, "Сохранённая сессия не расшифрована. Проверь "
                                        "TG_SESSION_ENCRYPTED и прежний DASHBOARD_PASSWORD.")
            try:
                string_session = StringSession(session)
            except (ValueError, TypeError):
                raise HTTPException(503, "Формат сохранённой сессии повреждён.")
            self.client = self.client_factory(
                string_session, int(self.settings.api_id), self.settings.api_hash,
                device_model="Personal Channel Manager on Render",
                app_version="0.1.0", lang_code="ru", system_lang_code="ru",
                receive_updates=False, catch_up=False, request_retries=1,
                connection_retries=1, flood_sleep_threshold=0,
            )
        if not self.client.is_connected():
            await self.client.connect()
        self.authorized = await self.client.is_user_authorized()
        return self.client

    async def status(self):
        if self.pending_until and self.pending_until < time.monotonic():
            self.clear_pending()
        if not self.settings.api_ready:
            return {"configured": False, "authorized": False,
                    "message": "Добавь TG_API_ID и TG_API_HASH в Environment на Render."}
        client = await self.ready()
        result = {"configured": True, "authorized": self.authorized,
                  "needs_password": self.needs_password,
                  "pending_code": bool(self.phone_code_hash) and self.pending_until > time.monotonic(),
                  "saved": bool(self.settings.encrypted_session) and self.authorized}
        setup_result = getattr(self, "channel_setup_result", None)
        if setup_result:
            result["channel_setup"] = setup_result
        if self.authorized:
            me = await client.get_me()
            if me is None:
                self.authorized = False
                raise HTTPException(409, "Сессия Telegram больше не действует. Начни вход заново.")
            result["account"] = {"id": me.id, "name": me.first_name or "Аккаунт",
                                 "username": me.username or ""}
        return result

    async def start(self, raw_phone):
        phone = re.sub(r"[\s()\-]", "", raw_phone)
        if re.fullmatch(r"\+[1-9][0-9]{6,14}", phone) is None:
            raise HTTPException(422, "Введи номер в международном формате с плюсом.")
        client = await self.ready()
        if self.authorized:
            return {"state": "authorized"}
        now = time.monotonic()
        if now - self.last_send < 60:
            raise HTTPException(429, "Подожди минуту перед новым запросом кода.",
                                headers={"Retry-After": str(int(61 - now + self.last_send))})
        self.last_send = now
        sent = await client.send_code_request(phone)
        self.phone = phone
        self.phone_code_hash = sent.phone_code_hash
        self.pending_until = time.monotonic() + 600
        self.needs_password = False
        self.code_attempts = 0
        self.password_attempts = 0
        return {"state": "code_sent"}

    async def code(self, raw_code):
        client = await self.ready()
        if self.authorized:
            return {"state": "authorized"}
        if self.needs_password:
            return {"state": "password_needed"}
        if not self.phone_code_hash or self.pending_until < time.monotonic():
            self.clear_pending()
            raise HTTPException(409, "Начни вход заново и запроси код.")
        if self.code_attempts >= 5:
            raise HTTPException(429, "Слишком много неверных кодов. Начни вход заново позже.")
        code = re.sub(r"\s", "", raw_code)
        if re.fullmatch(r"[0-9]{3,10}", code) is None:
            raise HTTPException(422, "Проверь код: введи только цифры.")
        self.code_attempts += 1
        try:
            await client.sign_in(phone=self.phone, code=code,
                                 phone_code_hash=self.phone_code_hash)
        except errors.SessionPasswordNeededError:
            self.needs_password = True
            self.pending_until = time.monotonic() + 600
            return {"state": "password_needed"}
        self.clear_pending()
        self.authorized = True
        return {"state": "authorized"}

    async def password(self, password):
        client = await self.ready()
        if self.authorized:
            return {"state": "authorized"}
        if not self.needs_password or self.pending_until < time.monotonic():
            self.clear_pending()
            raise HTTPException(409, "Время входа истекло. Запроси новый код.")
        if self.password_attempts >= 5:
            raise HTTPException(429, "Слишком много неверных паролей. Сделай паузу и начни вход заново позже.")
        self.password_attempts += 1
        await client.sign_in(password=password)
        self.clear_pending()
        self.authorized = True
        return {"state": "authorized"}

    def clear_pending(self):
        self.phone = self.phone_code_hash = None
        self.pending_until = 0
        self.needs_password = False
        self.code_attempts = 0
        self.password_attempts = 0

    async def export(self):
        client = await self.ready()
        if not self.authorized:
            raise HTTPException(409, "Сначала заверши вход в Telegram.")
        encrypted = await asyncio.to_thread(
            encrypt_session, client.session.save(), self.settings.dashboard_password)
        return {"encrypted_session": encrypted}

    async def close(self):
        if self.client is not None:
            await self.client.disconnect()


def create_app(settings=None, client_factory=TelegramClient):
    settings = settings or Settings.from_env()
    flow = LoginFlow(settings, client_factory)
    dashboard_sessions = {}
    unlock_attempts = deque()
    hostname = urlsplit(settings.origin).netloc
    logging.getLogger("telethon").setLevel(logging.CRITICAL)

    @asynccontextmanager
    async def lifespan(_app):
        setup_task = None
        setup_job = os.getenv("TG_CHANNEL_SETUP_JSON", "").strip()
        if setup_job:
            from channel_setup import run_configured_setup
            setup_task = asyncio.create_task(
                run_configured_setup(setup_job, flow, decrypt=decrypt_session))
        try:
            yield
        finally:
            if setup_task is not None:
                setup_task.cancel()
                with suppress(asyncio.CancelledError):
                    await setup_task
            await flow.close()

    app = FastAPI(title="Personal Telegram login", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.login_flow = flow

    @app.middleware("http")
    async def security(request, call_next):
        if request.url.path != "/health" and request.headers.get("host", "") != hostname:
            return JSONResponse({"detail": "Неверный адрес сервиса."}, status_code=400)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if request.headers.get("origin") != settings.origin:
                return JSONResponse({"detail": "Открой форму на адресе своего сервиса."}, status_code=403)
            if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
                return JSONResponse({"detail": "Неверный формат запроса."}, status_code=415)
            try:
                if int(request.headers.get("content-length", "0")) > 8192:
                    return JSONResponse({"detail": "Слишком большой запрос."}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "Неверный запрос."}, status_code=400)
        response = await call_next(request)
        response.headers.update({
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
                                       "img-src 'self' data:; connect-src 'self'; "
                                       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        })
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_fields(_request, _exc):
        return JSONResponse({"detail": "Проверь заполнение полей."}, status_code=422)

    def authorized(request, mutation=False):
        raw = request.cookies.get(COOKIE, "")
        key = hashlib.sha256(raw.encode()).hexdigest()
        saved = dashboard_sessions.get(key)
        if not saved or saved["expires"] < time.monotonic():
            dashboard_sessions.pop(key, None)
            raise HTTPException(401, "Введи пароль панели.")
        if mutation and not hmac.compare_digest(request.headers.get("x-csrf-token", ""), saved["csrf"]):
            raise HTTPException(403, "Обнови страницу и повтори вход в панель.")
        saved["expires"] = time.monotonic() + 3600
        return key, saved

    async def run(operation):
        async with flow.lock:
            try:
                return await asyncio.wait_for(operation(), timeout=35)
            except HTTPException:
                raise
            except errors.FloodWaitError as exc:
                seconds = max(1, int(exc.seconds))
                flow.blocked_until = time.monotonic() + seconds
                raise HTTPException(429, f"Telegram просит подождать {seconds} сек.",
                                    headers={"Retry-After": str(seconds)})
            except errors.PhoneCodeInvalidError:
                raise HTTPException(422, "Код не подошёл. Проверь его в Telegram.")
            except errors.PhoneCodeExpiredError:
                flow.clear_pending()
                raise HTTPException(409, "Код истёк. Запроси новый код позже.")
            except errors.PasswordHashInvalidError:
                raise HTTPException(422, "Пароль двухэтапной проверки не подошёл.")
            except errors.PhoneNumberInvalidError:
                raise HTTPException(422, "Telegram не принимает этот номер. Проверь формат.")
            except errors.PhoneNumberUnoccupiedError:
                raise HTTPException(422, "Для этого номера не найден существующий аккаунт.")
            except errors.ApiIdInvalidError:
                raise HTTPException(503, "Проверь пару TG_API_ID и TG_API_HASH в Render.")
            except (errors.AuthKeyUnregisteredError, errors.SessionRevokedError):
                flow.authorized = False
                raise HTTPException(409, "Сессия Telegram завершена. Начни вход заново; "
                                    "удали старый TG_SESSION_ENCRYPTED из Render.")
            except (TimeoutError, OSError):
                raise HTTPException(503, "Telegram сейчас не отвечает. Попробуй позже.")
            except errors.RPCError:
                raise HTTPException(502, "Telegram отклонил запрос. Не повторяй его часто; "
                                    "проверь аккаунт в официальном приложении.")

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/")
    async def index():
        return FileResponse(ROOT / "static" / "index.html")

    @app.get("/assets/app.js")
    async def javascript():
        return FileResponse(ROOT / "static" / "app.js", media_type="text/javascript")

    @app.get("/assets/style.css")
    async def styles():
        return FileResponse(ROOT / "static" / "style.css", media_type="text/css")

    @app.post("/api/unlock")
    async def unlock(body: Unlock):
        if not settings.password_ready:
            raise HTTPException(503, "Задай DASHBOARD_PASSWORD длиной от 24 символов в Render.")
        now = time.monotonic()
        while unlock_attempts and unlock_attempts[0] < now - 600:
            unlock_attempts.popleft()
        if len(unlock_attempts) >= 10:
            raise HTTPException(429, "Подожди 10 минут перед новой попыткой.",
                                headers={"Retry-After": "600"})
        unlock_attempts.append(now)
        supplied = hashlib.sha256(body.password.encode()).digest()
        expected = hashlib.sha256(settings.dashboard_password.encode()).digest()
        if not hmac.compare_digest(supplied, expected):
            raise HTTPException(401, "Пароль панели не подошёл.")
        unlock_attempts.clear()
        dashboard_sessions.clear()
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        dashboard_sessions[hashlib.sha256(token.encode()).hexdigest()] = {
            "csrf": csrf, "expires": now + 3600}
        response = JSONResponse({"csrf": csrf})
        response.set_cookie(COOKIE, token, max_age=3600, secure=True, httponly=True,
                            samesite="strict", path="/")
        return response

    @app.get("/api/status")
    async def status(request: Request):
        _, saved = authorized(request)
        result = await run(flow.status)
        result["csrf"] = saved["csrf"]
        return result

    @app.post("/api/login/start")
    async def start(request: Request, body: Phone):
        authorized(request, mutation=True)
        return await run(lambda: flow.start(body.phone))

    @app.post("/api/login/code")
    async def code(request: Request, body: Code):
        authorized(request, mutation=True)
        return await run(lambda: flow.code(body.code))

    @app.post("/api/login/password")
    async def password(request: Request, body: Password):
        authorized(request, mutation=True)
        return await run(lambda: flow.password(body.password))

    @app.post("/api/session/export")
    async def export(request: Request):
        authorized(request, mutation=True)
        return await run(flow.export)

    @app.post("/api/lock")
    async def lock(request: Request):
        key, _ = authorized(request, mutation=True)
        dashboard_sessions.pop(key, None)
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="strict")
        return response

    return app


app = create_app()
