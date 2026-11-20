"""Create an independent Telegram session inside Termux's private storage.

This onboarding step verifies access to the owner's existing channel. It does
not post, subscribe to channels, read personal conversations or enable a worker.
"""

import asyncio
from getpass import getpass
import json
import logging
import os
from pathlib import Path
import re
import sys

from telethon import TelegramClient, errors, types


DATA_DIR = Path.home() / ".local" / "share" / "che-tam"
SETTINGS_FILE = DATA_DIR / "settings.json"
CHANNEL_ID = 4475667930
CHANNEL_USERNAME = "che_tam_cheboksary"


def valid_api_pair(api_id, api_hash):
    return (isinstance(api_id, str) and api_id.isascii() and api_id.isdigit()
            and int(api_id) > 0 and isinstance(api_hash, str)
            and re.fullmatch(r"[0-9a-fA-F]{32}", api_hash) is not None)


def save_settings(settings):
    temporary = SETTINGS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(SETTINGS_FILE)


def load_settings():
    DATA_DIR.mkdir(parents=True, mode=0o700, exist_ok=True)
    DATA_DIR.chmod(0o700)
    if SETTINGS_FILE.is_file():
        value = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("SETTINGS_FORMAT")
        return value
    return {}


async def setup():
    settings = load_settings()
    if not valid_api_pair(settings.get("api_id"), settings.get("api_hash")):
        print("Открой my.telegram.org → API development tools.")
        print("Нужны api_id и api_hash уже созданного приложения.")
        api_id = input("App api_id: ").strip()
        api_hash = getpass("App api_hash (ввод скрыт): ").strip()
        if not valid_api_pair(api_id, api_hash):
            print("Проверь значения: api_id — число, api_hash — 32 символа.")
            return 1
        settings.update(api_id=api_id, api_hash=api_hash)
        save_settings(settings)

    client = TelegramClient(
        str(DATA_DIR / "phone_account"), int(settings["api_id"]), settings["api_hash"],
        device_model="POCO Channel Manager", app_version="0.1.0", lang_code="ru",
        system_lang_code="ru", receive_updates=False, catch_up=False,
        connection_retries=1, request_retries=0, flood_sleep_threshold=0)
    client.session.save_entities = False
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.start(
                phone=lambda: getpass("Номер Telegram с кодом страны, начиная с + (ввод скрыт): ").strip(),
                code_callback=lambda: getpass("Код из Telegram (ввод скрыт): ").strip(),
                password=lambda: getpass("Пароль двухэтапной проверки (ввод скрыт): "),
                max_attempts=3)
        target = await client.get_entity(CHANNEL_USERNAME)
        if (not isinstance(target, types.Channel) or target.id != CHANNEL_ID
                or not target.creator or not target.broadcast):
            print("Этот аккаунт не владеет нужным каналом. Автоматизация не включена.")
            return 1
        settings.update(channel_id=CHANNEL_ID, channel_username=CHANNEL_USERNAME,
                        channel_title=target.title, phone_session_ready=True,
                        automation_enabled=False, budget="free")
        save_settings(settings)
        print("\nГотово: телефон подключён к Telegram.")
        print(f"Канал: {target.title} — https://t.me/{CHANNEL_USERNAME}")
        print("Публикации не отправлены. Сбор новостей пока не включён.")
        print("Ключи и сессия сохранены только внутри Termux.")
        return 0
    finally:
        await client.disconnect()


def main():
    os.umask(0o077)
    logging.getLogger("telethon").setLevel(logging.CRITICAL)
    try:
        return asyncio.run(setup())
    except errors.FloodWaitError as exc:
        print(f"Telegram просит подождать {max(1, int(exc.seconds))} секунд.")
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nНастройка остановлена.")
        return 1
    except Exception as exc:
        # Exception text may contain user-provided values. Only print its type.
        print(f"Не удалось завершить настройку: {type(exc).__name__}.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
