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
from telegram_sources import CLAUSES, EXERCISE, START, STOP, Source, local_headline, select_alert


KEY_FILE = DATA_DIR / "tmaks.key"
API_BASE = "https://tmaks.ru/api/v1/integrations"
MAX_INTERVAL = 90
MAX_SOURCES = {
    "id2126003194_gos2": "Станислав Трофимов",
    "id2128054356_gos": "Минобразования Чувашии",
}
MAX_ALERT_SOURCES = {
    "id2127305504_gos": "МЧС Чувашии",
    "id2130213317_gos": "РСЧС Чувашская Республика",
}
NEIGHBOR_SOURCES = {
    "id1660025250_gos": ("РСЧС Татарстана", "Татарстан", "tatarstan", r"\bтатарстан[аеу]?\b"),
    "officialmordovia": ("Республика Мордовия", "Мордовия", "mordovia", r"\bмордов(?:ия|ии|ию|ией)\b"),
    "id5260494537_gos": ("РСЧС Нижегородской области", "Нижегородская область", "nizhny-novgorod",
                          r"\bнижегородск\w+\s+област\w*\b"),
}
NEIGHBOR_HAZARDS = {
    "drone": ("беспилотная опасность", r"\b(?:беспилотн\w+\s+опасност\w*|"
              r"(?:опасност\w*|угроз\w*)\s+(?:атаки\s+)?бпла)\b"),
    "rocket": ("ракетная опасность", r"\bракетн\w+\s+опасност\w*\b"),
}
NEIGHBOR_NON_NOTICE = re.compile(
    r"\b(?:не|возможно|возможн\w+|может|риск\w*|вероятност\w*|ожида\w*|вчера|"
    r"памятк\w*|учени\w*|проверк\w*|при угрозе|в случае|напоминаем|"
    r"фиксац\w*|транзит\w*|в направлении|в сторону)\b", re.I,
)
MAX_ALERT_AGE = timedelta(minutes=12)
KEY_FORMAT = re.compile(r"tmk_[A-Za-z0-9_-]+")
MINISTER_BYLINE = re.compile(r"^\s*Дмитрий Захаров\s*:\s*", re.I)


@dataclass(frozen=True)
class MaxPost:
    source: str
    url: str
    published: datetime
    title: str
    has_visual: bool
    signal: str | None = None
    phase: str | None = None


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


def neighbor_notice(neighbor, text):
    name, region, region_id, area = neighbor
    notices = []
    for clause in CLAUSES.split(" ".join(text[:350].split())):
        if not re.search(area, clause, re.I) or EXERCISE.search(clause) or NEIGHBOR_NON_NOTICE.search(clause):
            continue
        started, stopped = START.search(clause), STOP.search(clause)
        if bool(started) == bool(stopped):
            continue
        for hazard, (label, pattern) in NEIGHBOR_HAZARDS.items():
            if re.search(pattern, clause, re.I):
                phase = "отбой" if stopped else "тревога"
                status = "отбой" if stopped else "действует"
                title = f"Соседние регионы · {region}\n{label.capitalize()} — {status}"
                notices.append((title, f"official:{region_id}:{hazard}", phase))
    # A single post must unambiguously announce one status and one hazard.
    return (name, *notices[0]) if len(notices) == 1 else None


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
    if not isinstance(handle, str):
        return None
    handle = handle.casefold().lstrip("@")
    neighbor = NEIGHBOR_SOURCES.get(handle)
    name = neighbor[0] if neighbor else MAX_ALERT_SOURCES.get(handle) or MAX_SOURCES.get(handle)
    if name is None:
        return None
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
    if neighbor:
        if now - published > MAX_ALERT_AGE:
            return None
        official = neighbor_notice(neighbor, text)
        if official:
            name, title, signal, phase = official
            return MaxPost(name, link, published, title, False, signal, phase)
        return None
    if handle in MAX_ALERT_SOURCES and now - published <= MAX_ALERT_AGE:
        selected = select_alert(Source(handle, name, "official", MAX_INTERVAL), text)
        if selected:
            _, title, signal, phase = selected
            return MaxPost(name, link, published, title, False, signal, phase)
    if handle == "id2128054356_gos":
        text = MINISTER_BYLINE.sub("", text, count=1)
    title = local_headline(text)
    media = message.get("media")
    has_visual = isinstance(media, list) and any(isinstance(item, dict)
        and item.get("type") in ("photo", "video") for item in media)
    return MaxPost(name, link, published, title, has_visual) if title else None


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
        print("Добавь правила мониторинга для источников MAX в ТелеМаксе.")
    print("Запущенный сборщик подхватит ключ автоматически.")
    return 0


if __name__ == "__main__":
    raise SystemExit(save_key())
