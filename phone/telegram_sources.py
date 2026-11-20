"""Select local Telegram posts using their source, wording and timestamp."""

from dataclasses import dataclass
from datetime import timedelta
import re

from news import AIR_ALERT, CITY, MSK, is_interesting


@dataclass(frozen=True)
class Source:
    handle: str
    name: str
    kind: str
    interval: int


SOURCES = (
    Source("mchs_chuvashia", "МЧС Чувашии", "official", 60),
    Source("oanikolaev", "Глава Чувашии", "official", 60),
    Source("stanisk92", "Станислав Трофимов", "city", 60),
    Source("vrv_radar", "Радар ВРВ", "radar", 45),
    Source("gcheb", "Про Чебоксары · Администрация города", "local", 600),
    Source("minobr21", "Минобразования Чувашии", "local", 600),
    Source("mintrans21", "Минтранс Чувашии", "local", 600),
    Source("chebmedia", "Cheb.media", "local", 600),
    Source("vkratce_cheboksary", "Как-то так | Чебоксары", "local", 600),
    Source("zarulemws", "Автоновости Чебоксары от ZR", "local", 600),
    Source("chsptgcheb", "ЧСП | Чебоксары", "local", 600),
    Source("chuvashiya_121", "Первый чувашский", "local", 600),
    Source("pg21ru", "Про Город Чебоксары", "local", 600),
)
EXERCISE = re.compile(
    r"\b(?:учебн\w*|планов\w*\s+проверк\w*|тренировк\w*|памятк\w*|"
    r"при получении сигнала|что делать при|как действовать при)\b", re.I
)
ROCKET = re.compile(r"\b(?:ракетн\w*\s+опасност\w*|ракет[а-яё]*)\b", re.I)
DRONE = re.compile(r"\b(?:бпла|беспилотн\w*)\b", re.I)
DETECTION = re.compile(r"\b(?:фиксац\w*|фиксир\w*|зафиксир\w*|обнаруж\w*)\b", re.I)
STOP = re.compile(r"\b(?:отбой|отмен[её]н[ао]?|снят[ао]?)\b", re.I)
START = re.compile(r"\b(?:объявлен[ао]?|введ[её]н[ао]?|вводится|действует|сохраняется)\b", re.I)
NEGATED = re.compile(r"\b(?:не|будет|будут|может|могут|планируют)\b", re.I)
CLAUSES = re.compile(r"[.!?;]+|,\s*(?:но|однако|при этом)\s+", re.I)
PROMO = re.compile(r"\b(?:реклама|розыгрыш|промокод|подпишись|подпишитесь)\b", re.I)
GREETING = re.compile(r"^(?:дорогие|уважаемые|друзья|добр(?:ое|ый)\s+)", re.I)


def headline(text):
    first = ""
    for line in text.splitlines():
        clean = re.sub(r"https?://\S+|@\w+", "", line).strip()
        if clean and not GREETING.search(clean):
            first = clean
            break
    first = " ".join(first.split()[:20])
    if len(first) > 155:
        first = first[:152].rsplit(" ", 1)[0] + "…"
    return first


def local_headline(text):
    first = headline(text)
    if (len(first) >= 18 and CITY.search(text[:750]) and not PROMO.search(first)
            and not AIR_ALERT.search(text) and is_interesting(first, text)):
        for old, new in (("На территории города Чебоксары", "В Чебоксарах"),
                         ("будет произведено отключение", "отключат"),
                         ("движение будет ограничено", "движение ограничат"),
                         ("проведут работы по замене", "заменят")):
            first = first.replace(old, new)
        return first
    return None


def select_message(source, message, now):
    """Return (kind, title, source-state key, phase) for a fresh, relevant message."""
    text = (getattr(message, "message", "") or "").strip()
    if not text or getattr(message, "fwd_from", None) or message.date is None:
        return None
    age = now - message.date
    if not timedelta(seconds=-30) <= age <= timedelta(hours=6):
        return None
    if source.kind != "local" and age <= timedelta(minutes=8):
        alert = select_alert(source, text)
        if alert:
            return alert
    if source.kind in ("local", "city"):
        # Preserve a short excerpt and its link as a source card.
        first = local_headline(text)
        if first:
            return "local", first, None, None
    return None


def select_alert(source, text):
    # Only look at the announcement at the top, never at a later quote or reminder.
    top = " ".join(text[:600].split())
    if not CITY.search(top):
        return None
    if source.kind == "radar":
        if EXERCISE.search(top):
            return None
        for sentence in CLAUSES.split(top):
            if (CITY.search(sentence) and DETECTION.search(sentence)
                    and (ROCKET.search(sentence) or DRONE.search(sentence))):
                rocket, drone = ROCKET.search(sentence), DRONE.search(sentence)
                topic = "ракетах и БПЛА" if rocket and drone else "ракетах" if rocket else "БПЛА"
                tag = "rocket+drone" if rocket and drone else "rocket" if rocket else "drone"
                title = f"📡 «Радар ВРВ» сообщил о {topic} с упоминанием Чувашии"
                return "radar", title, f"radar:{tag}", None
        return None

    for sentence in CLAUSES.split(top):
        if (not CITY.search(sentence) or not AIR_ALERT.search(sentence)
                or EXERCISE.search(sentence) or NEGATED.search(sentence)):
            continue
        state = "отбой" if STOP.search(sentence) else "тревога" if START.search(sentence) else None
        if not state:
            continue
        rocket, drone = ROCKET.search(sentence), DRONE.search(sentence)
        hazard = "rocket+drone" if rocket and drone else "rocket" if rocket else "drone" if drone else "air"
        return "official", "🔔 Официальное сообщение для Чувашии", f"official:{hazard}", state
    return None


def post_time(message):
    return message.date.astimezone(MSK).strftime("%d.%m в %H:%M мск")
