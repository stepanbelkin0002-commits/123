"""Read public MAX posts through the documented Telemaks monitoring feed."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from getpass import getpass
import json
import os
import re
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener

from news import NoRedirect
from setup import DATA_DIR
from telegram_sources import local_headline


KEY_FILE = DATA_DIR / "tmaks.key"
API_BASE = "https://tmaks.ru/api/v1/integrations"
MAX_INTERVAL = 90
MAX_SOURCES = {
    "id2126003194_gos2": "Станислав Трофимов",
    "id2128054356_gos": "Минобразования Чувашии",
}
KEY_FORMAT = re.compile(r"tmk_[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class MaxPost:
    source: str
    url: str
    published: datetime
    title: str
    has_visual: bool


def get_list(key, endpoint, **params):
    query = "?" + urlencode(params) if params else ""
    request = Request(f"{API_BASE}/{endpoint}{query}", headers={"X-API-Key": key})
    with build_opener(NoRedirect()).open(request, timeout=12) as response:
        raw = response.read(4_000_001)
    if len(raw) > 4_000_000:
        raise ValueError("Ответ ТелеМакса слишком большой")
    result = json.loads(raw)
    if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
        raise ValueError("Формат ленты ТелеМакса изменился")
    return result


def fetch_events(key, cursor):
    params = {"type": "monitoring.hit.created", "limit": 100}
    if cursor:
        params["since"] = cursor
    return get_list(key, "events", **params)


def select_post(event, now, enabled_at):
    if event.get("type") != "monitoring.hit.created":
        return None
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    source, message = data.get("source"), data.get("message")
    if not isinstance(source, dict) or source.get("platform") != "max" or not isinstance(message, dict):
        return None
    handle = source.get("username")
    if not isinstance(handle, str) or handle.casefold().lstrip("@") not in MAX_SOURCES:
        return None
    handle = handle.casefold().lstrip("@")
    link, timestamp, text = (message.get(field) for field in ("link", "published_at", "text"))
    if not all(isinstance(value, str) for value in (link, timestamp, text)):
        return None
    try:
        parts = urlsplit(link)
        published = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if (parts.scheme != "https" or parts.netloc != "max.ru" or parts.query or parts.fragment
            or not re.fullmatch(rf"/{handle}/[A-Za-z0-9_-]{{1,64}}", parts.path)):
        return None
    if (published.tzinfo is None or published < enabled_at
            or not now - timedelta(hours=6) <= published <= now + timedelta(seconds=30)):
        return None
    title = local_headline(text)
    media = message.get("media")
    has_visual = isinstance(media, list) and any(isinstance(item, dict)
        and item.get("type") in ("photo", "video") for item in media)
    return MaxPost(MAX_SOURCES[handle], link, published, title, has_visual) if title else None


def save_key():
    key = getpass("Ключ ТелеМакса (ввод скрыт): ").strip()
    if not KEY_FORMAT.fullmatch(key):
        print("Нужен ключ API проекта ТелеМакса. Он начинается с tmk_.")
        return 1
    try:
        rules = get_list(key, "rules")
    except HTTPError as exc:
        print(f"ТелеМакс — HTTP {exc.code}. Проверь доступ и ключ проекта.")
        return 1
    except (OSError, ValueError) as exc:
        print(f"ТелеМакс недоступен — {type(exc).__name__}")
        return 1
    os.umask(0o077)
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = KEY_FILE.with_suffix(".tmp")
    temporary.write_text(key + "\n", encoding="ascii")
    temporary.chmod(0o600)
    temporary.replace(KEY_FILE)
    enabled = sum(rule.get("enabled") is True for rule in rules)
    print(f"Ключ принят. Активных правил мониторинга в ТелеМаксе: {enabled}.")
    if not enabled:
        print("Добавь правила мониторинга для двух источников MAX в ТелеМаксе.")
    print("Запущенный сборщик подхватит ключ автоматически.")
    return 0


if __name__ == "__main__":
    raise SystemExit(save_key())
