"""Run the Cheboksary channel from a phone's existing Termux session."""

import argparse
import asyncio
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
from html import escape
import json
import logging
import os
from pathlib import Path
import signal
import sys
from time import monotonic, sleep
from urllib.error import HTTPError

from telethon import TelegramClient, errors, functions, types, utils
from telethon.extensions import html as telegram_html
from telethon.network.connection import ConnectionTcpObfuscated

from max_sources import KEY_FILE as MAX_KEY_FILE, MAX_ALERT_AGE, MAX_INTERVAL, fetch_events, select_post
from news import DraftError, FEEDS, MSK, fetch_feed, make_draft, select_articles, similar_title
from news_media import fetch_visual
from setup import CHANNEL_ID, CHANNEL_USERNAME, DATA_DIR, load_settings, valid_api_pair
from telegram_sources import SOURCES, post_time, select_message


KEY_FILE = DATA_DIR / "openrouter.key"
STATE_FILE = DATA_DIR / "news-state.json"
LOCK_FILE = DATA_DIR / "worker.lock"
HEALTH_FILE = DATA_DIR / "health.json"
VERSION = "2026.11.21.4"
ALREADY_PUBLISHED = {
    "https://pg21.ru/news/132009",
    "https://moygorod.online/society/society_100565.html",
}
FEED_INTERVAL = 15 * 60
REGULAR_INTERVAL = 45 * 60


def log(message):
    print(f"[{datetime.now(MSK):%d.%m %H:%M:%S}] {message}", flush=True)


def describe_error(error):
    if isinstance(error, DraftError):
        return str(error)
    if isinstance(error, HTTPError):
        if error.code == 401:
            return "HTTP 401 — ключ не принят"
        if error.code == 403:
            return "HTTP 403 — сервер запретил запрос"
        if error.code == 429:
            return "HTTP 429 — достигнут лимит запросов"
        return f"HTTP {error.code}"
    if isinstance(error, errors.FloodWaitError):
        return f"Telegram просит подождать {error.seconds} сек."
    return type(error).__name__


def now_utc():
    return datetime.now(timezone.utc)


class State:
    def __init__(self):
        self.data = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
        self.sources = self.data.setdefault("sources", {})
        self.recent = self.data.setdefault("recent", [])
        self.signals = self.data.setdefault("signals", {})
        self.alert_lock = asyncio.Lock()
        self.leads = self.data.setdefault("leads", [])
        self.pending = self.data.setdefault("pending", [])
        self.regular_lock = asyncio.Lock()

    def save(self):
        temporary = STATE_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        temporary.replace(STATE_FILE)

    def seen_story(self, title):
        return any(similar_title(title, item["title"]) for item in self.recent[-120:])

    def regular_due(self, now):
        local = now.astimezone(MSK)
        if not 8 <= local.hour < 22:
            return False
        today = local.date().isoformat()
        if sum(item.get("day") == today for item in self.recent) >= 6:
            return False
        return now.timestamp() - self.data.get("last_regular", 0) >= REGULAR_INTERVAL

    def remember_lead(self, title, now):
        if title not in self.lead_titles(now):
            self.leads.append({"title": title, "time": now.timestamp()})
            del self.leads[:-80]

    def lead_titles(self, now):
        return [lead["title"] for lead in self.leads if now.timestamp() - lead["time"] <= 6 * 3600]

    def queue_story(self, source, channel, message, title):
        url = f"https://t.me/{source.handle}/{message.id}"
        if (self.seen_story(title) or any(item["url"] == url for item in self.pending)
                or any(item["url"] == url for item in self.recent)):
            return
        self.pending.append({"handle": source.handle, "channel_id": channel.id,
                             "message_id": message.id, "url": url, "title": title,
                             "time": message.date.timestamp(),
                             "has_media": bool(getattr(message, "photo", None)
                                               or getattr(message, "video", None))})
        del self.pending[:-80]

    def expire_pending(self, now):
        self.pending[:] = [item for item in self.pending
                           if now.timestamp() - item["time"] <= 6 * 3600]

    def remember(self, url, title, now):
        self.recent.append({"url": url, "title": title, "day": now.astimezone(MSK).date().isoformat()})
        del self.recent[:-120]
        self.data["last_regular"] = now.timestamp()
        self.save()


def random_id(key):
    return int.from_bytes(sha256(f"{CHANNEL_ID}/{key}".encode()).digest()[:8], "big", signed=True) or 1


async def send_post(client, target, key, html):
    message, entities = telegram_html.parse(html)
    try:
        await asyncio.wait_for(client(functions.messages.SendMessageRequest(
            peer=utils.get_input_peer(target), message=message, entities=entities,
            random_id=random_id(key), no_webpage=True)), 45)
    except errors.RandomIdDuplicateError:
        log("Telegram уже принял эту запись")


async def forward_official(client, target, origin, message):
    key = f"official:{origin.id}:{message.id}"
    try:
        await asyncio.wait_for(client(functions.messages.ForwardMessagesRequest(
            from_peer=utils.get_input_peer(origin), id=[message.id], random_id=[random_id(key)],
            to_peer=utils.get_input_peer(target))), 45)
    except errors.RandomIdDuplicateError:
        log("Telegram уже принял это сообщение")


async def publish_alert(client, target, state, signal, phase, published, url, caption,
                        *, telegram_message=None, telegram_channel=None):
    async with state.alert_lock:
        previous = state.signals.get(signal, {})
        timestamp = published.timestamp()
        last_time = previous.get("time", 0)
        if timestamp <= last_time or (phase == previous.get("phase") and timestamp - last_time < 300):
            return False
        if telegram_message is None:
            if now_utc() - published > MAX_ALERT_AGE:
                return False
            await send_post(client, target, url, caption)
        else:
            try:
                await forward_official(client, target, telegram_channel, telegram_message)
            except errors.ChatForwardsRestrictedError:
                await send_post(client, target, url, caption)
        state.signals[signal] = {"time": timestamp, "phase": phase}
        state.save()
        log(f"Официальное сообщение опубликовано — {url}")
        return True


async def publish_regular(client, target, state, url, title, caption, media):
    async with state.regular_lock:
        now = now_utc()
        if (not state.regular_due(now) or state.seen_story(title)
                or any(item["url"] == url for item in state.recent)):
            return None
        text, entities = telegram_html.parse(caption)
        try:
            if media is None:
                await send_post(client, target, url, caption)
            else:
                await asyncio.wait_for(client(functions.messages.SendMediaRequest(
                    peer=utils.get_input_peer(target), media=media, message=text,
                    entities=entities, random_id=random_id(url))), 45)
        except errors.RandomIdDuplicateError:
            pass
        except (errors.FileReferenceExpiredError, errors.ChatForwardsRestrictedError,
                errors.MediaInvalidError, errors.MediaEmptyError, errors.WebpageCurlFailedError,
                errors.WebpageMediaEmptyError):
            log(f"Фото или видео недоступно, публикуется текст — {url}")
            await send_post(client, target, url, caption)
        except errors.BadRequestError as exc:
            if exc.message != "WEBPAGE_NOT_FOUND":
                raise
            log(f"Превью недоступно, публикуется текст — {url}")
            await send_post(client, target, url, caption)
        state.remember(url, title, now)
        log(f"Пост опубликован — {url}")
        return True


async def get_broadcast(client, handle):
    result = await client.get_entity("@" + handle)
    usernames = {value.username.casefold() for value in (getattr(result, "usernames", None) or [])
                 if value.active}
    if (not isinstance(result, types.Channel) or not result.broadcast
            or handle.casefold() not in (usernames | {(result.username or "").casefold()})):
        raise ValueError("Нужен публичный канал")
    return result


async def get_feeds():
    now = now_utc()
    results = await asyncio.gather(
        *(asyncio.to_thread(fetch_feed, name, url, now) for name, url in FEEDS),
        return_exceptions=True)
    feeds = []
    for (name, _), result in zip(FEEDS, results):
        if isinstance(result, Exception):
            log(f"{name} — {describe_error(result)}")
        else:
            log(f"{name} — свежих материалов {len(result)}")
            feeds.append(result)
    return feeds


def regular_caption(title, url, body=""):
    heading = f'<b><a href="{escape(url, quote=True)}">{escape(title)}</a></b>'
    return heading + "\n\n" + escape(body) if body else heading


def telegram_media(channel, message):
    if getattr(channel, "noforwards", False) or getattr(message, "noforwards", False):
        return None
    media = getattr(message, "media", None)
    if getattr(media, "ttl_seconds", None) or not (getattr(message, "photo", None)
                                                 or getattr(message, "video", None)):
        return None
    return utils.get_input_media(media)


def web_preview(url):
    return types.InputMediaWebPage(url=url, force_large_media=True, optional=False)


async def articles_with_visuals(articles):
    visuals = await asyncio.gather(*(asyncio.to_thread(fetch_visual, article.url) for article in articles))
    paired = list(zip(articles, visuals))
    return [article for article, _ in sorted(paired, key=lambda pair: bool(pair[1]), reverse=True)]


async def preview_news(key):
    feeds = await get_feeds()
    articles = await articles_with_visuals(select_articles(feeds, ALREADY_PUBLISHED, []))
    if not articles:
        log("Подходящих свежих сюжетов для черновика нет")
        return
    if not key:
        log("Для черновиков нужен ключ OpenRouter")
        return
    try:
        draft = await asyncio.to_thread(make_draft, key, articles)
    except Exception as exc:
        log(f"OpenRouter — {describe_error(exc)}")
        return
    if draft is None:
        log("ИИ прочитал ленты и пропустил текущую подборку")
    else:
        log("Черновик")
        print(telegram_html.parse(regular_caption(draft.title, draft.article.url, draft.body))[0], flush=True)
        log(f"Публикация — {draft.article.url}")


async def preview_sources(client):
    for source in SOURCES:
        try:
            channel = await asyncio.wait_for(get_broadcast(client, source.handle), 12)
            messages = await asyncio.wait_for(client.get_messages(channel, limit=1), 12)
            time = post_time(messages[0]) if messages else "нет публикаций"
            log(f"@{source.handle} — доступен, последняя запись {time}")
        except errors.FloodWaitError as exc:
            log(describe_error(exc))
            break
        except Exception as exc:
            log(f"@{source.handle} — {describe_error(exc)}")


async def publish_news(client, target, state, key):
    while True:
        try:
            if key and state.regular_due(now_utc()):
                feeds = await get_feeds()
                urls = ALREADY_PUBLISHED | {item["url"] for item in state.recent}
                articles = await articles_with_visuals(select_articles(
                    feeds, urls, [item["title"] for item in state.recent], state.lead_titles(now_utc())))
                if not articles:
                    log("Сайты проверены — свежих подходящих сюжетов пока нет")
                try:
                    draft = await asyncio.to_thread(make_draft, key, articles)
                except (HTTPError, DraftError) as exc:
                    log(f"OpenRouter — {describe_error(exc)}")
                    draft = None
                if draft is not None:
                    url = draft.article.url
                    await publish_regular(client, target, state, url, draft.article.title,
                                          regular_caption(draft.title, url, draft.body), web_preview(url))
                elif articles:
                    log("ИИ пропустил подборку — повторная проверка через 15 минут")
        except errors.FloodWaitError as exc:
            log(describe_error(exc))
            await asyncio.sleep(max(1, exc.seconds))
        except Exception as exc:
            log(f"Новостные сайты или ИИ — {describe_error(exc)}")
        await asyncio.sleep(FEED_INTERVAL)


async def poll_source(client, target, state, source, channel):
    entry = state.sources.get(source.handle)
    if entry is None:
        last = await client.get_messages(channel, limit=5)
        state.sources[source.handle] = {"channel_id": channel.id, "last_id": last[0].id if last else 0}
        for message in reversed(last):
            selected = select_message(source, message, now_utc())
            if selected and selected[0] == "local":
                state.queue_story(source, channel, message, selected[1])
        state.save()
        log(f"@{source.handle} — отслеживаются новые сообщения")
        return
    if entry["channel_id"] != channel.id:
        raise ValueError("Канал источника изменился")
    recent = await client.get_messages(channel, limit=60)
    messages = sorted((message for message in recent if message.id > entry["last_id"]),
                      key=lambda message: message.id)
    now = now_utc()
    for message in messages:
        selected = select_message(source, message, now)
        if selected:
            kind, title, signal, phase = selected
            url = f"https://t.me/{source.handle}/{message.id}"
            if kind == "local":
                state.remember_lead(title, now)
                state.queue_story(source, channel, message, title)
            elif kind == "official":
                text = (f"<b>{escape(title)}</b>\n\n"
                        f'<a href="{url}">{escape(source.name)}</a> · {post_time(message)}')
                await publish_alert(client, target, state, signal, phase, message.date, url, text,
                                    telegram_message=message, telegram_channel=channel)
            elif now.timestamp() - state.signals.get(signal, 0) >= 300:
                text = (f"<b>{escape(title)}</b>\n\n"
                        f"Сообщение мониторингового канала · {post_time(message)}\n\n"
                        f'<a href="{url}">Оригинал сообщения</a>')
                await send_post(client, target, url, text)
                state.signals[signal] = now.timestamp()
                log(f"Запись радара опубликована — {url}")
        entry["last_id"] = message.id
        state.save()


async def watch_telegram(client, target, state):
    entities = {}
    due = {source.handle: 0 for source in SOURCES}
    while True:
        if not client.is_connected():
            try:
                await asyncio.wait_for(client.disconnect(), 10)
                await asyncio.wait_for(client.connect(), 25)
            except Exception as exc:
                log(f"Подключение к Telegram — {describe_error(exc)}")
                await asyncio.sleep(20)
                continue
        for source in SOURCES:
            if due[source.handle] > monotonic():
                continue
            try:
                channel = entities.get(source.handle)
                if channel is None:
                    channel = await asyncio.wait_for(get_broadcast(client, source.handle), 12)
                    entities[source.handle] = channel
                await asyncio.wait_for(poll_source(client, target, state, source, channel), 16)
                due[source.handle] = monotonic() + source.interval
            except errors.FloodWaitError as exc:
                log(describe_error(exc))
                await asyncio.sleep(max(1, exc.seconds))
                break
            except Exception as exc:
                log(f"@{source.handle} — {describe_error(exc)}")
                due[source.handle] = monotonic() + max(60, source.interval)
            # Check urgent sources again between ordinary channels.
            if source.kind == "local":
                break
        await asyncio.sleep(2)


async def publish_queue(client, target, state):
    sources = {source.handle: source for source in SOURCES}
    while True:
        try:
            state.expire_pending(now_utc())
            if state.pending and state.regular_due(now_utc()):
                entry = max(state.pending, key=lambda item: (item.get("has_media", False), item["time"]))
                source = sources.get(entry["handle"])
                if (source is None or state.seen_story(entry["title"])
                        or any(item["url"] == entry["url"] for item in state.recent)):
                    state.pending.remove(entry)
                else:
                    channel = await asyncio.wait_for(get_broadcast(client, source.handle), 12)
                    if channel.id != entry["channel_id"]:
                        raise ValueError("Канал источника изменился")
                    message = await asyncio.wait_for(client.get_messages(channel, ids=entry["message_id"]), 12)
                    selected = select_message(source, message, now_utc()) if message else None
                    if not selected or selected[0] != "local":
                        state.pending.remove(entry)
                    else:
                        title = selected[1]
                        result = await publish_regular(client, target, state, entry["url"], title,
                                                       regular_caption(title, entry["url"]),
                                                       telegram_media(channel, message))
                        if result:
                            state.pending.remove(entry)
            state.save()
        except errors.FloodWaitError as exc:
            log(describe_error(exc))
            await asyncio.sleep(max(1, exc.seconds))
        except Exception as exc:
            log(f"Очередь новостей — {describe_error(exc)}")
        await asyncio.sleep(30)


async def heartbeat(client, state):
    while True:
        payload = {"version": VERSION, "pid": os.getpid(), "time": now_utc().isoformat(),
                   "telegram_connected": client.is_connected(), "pending": len(state.pending),
                   "last_regular": state.data.get("last_regular"), "sources": len(state.sources)}
        temporary = HEALTH_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(HEALTH_FILE)
        await asyncio.sleep(60)


async def watch_max(client, target, state):
    feed = state.data.setdefault("max_feed", {})
    connected = False
    while True:
        try:
            if MAX_KEY_FILE.is_file():
                key = MAX_KEY_FILE.read_text(encoding="ascii").strip()
                fingerprint = sha256(key.encode("ascii")).hexdigest()[:16]
                if feed.get("key_id") != fingerprint:
                    feed.clear()
                    feed.update(key_id=fingerprint, enabled_at=now_utc().isoformat())
                    connected = False
                    state.save()
                enabled_at = datetime.fromisoformat(feed["enabled_at"])
                events = await asyncio.to_thread(fetch_events, key, feed.get("cursor"))
                if not connected:
                    log("MAX — лента ТелеМакса доступна")
                    connected = True
                posts = [select_post(event, now_utc(), enabled_at) for event in events]
                latest_alerts = {}
                for post in posts:
                    if post and post.signal:
                        latest = latest_alerts.get(post.signal)
                        if latest is None or post.published > latest.published:
                            latest_alerts[post.signal] = post
                for post in sorted(latest_alerts.values(), key=lambda item: item.published):
                    stamp = post.published.astimezone(MSK).strftime("%d.%m в %H:%M мск")
                    caption = (f"<b>{escape(post.title)}</b>\n\n"
                               f'<a href="{escape(post.url, quote=True)}">{escape(post.source)} в MAX</a>'
                               f" · {stamp}")
                    await publish_alert(client, target, state, post.signal, post.phase,
                                        post.published, post.url, caption)
                for event, post in zip(events, posts):
                    cursor = event.get("cursor")
                    if not isinstance(cursor, str) or not 1 <= len(cursor) <= 256:
                        raise ValueError("Нет курсора события ТелеМакса")
                    if post and not post.signal:
                        state.remember_lead(post.title, now_utc())
                        if post.has_visual and state.regular_due(now_utc()):
                            if await asyncio.to_thread(fetch_visual, post.url):
                                await publish_regular(client, target, state, post.url, post.title,
                                                      regular_caption(post.title, post.url), web_preview(post.url))
                    feed["cursor"] = cursor
                if events:
                    state.save()
        except errors.FloodWaitError as exc:
            log(describe_error(exc))
            await asyncio.sleep(max(1, exc.seconds))
        except Exception as exc:
            log(f"MAX / ТелеМакс — {describe_error(exc)}")
        await asyncio.sleep(MAX_INTERVAL)


async def run(mode, settings, key):
    client = TelegramClient(
        str(DATA_DIR / "phone_account"), int(settings["api_id"]), settings["api_hash"],
        device_model="POCO Channel Manager", app_version="1.0", lang_code="ru",
        system_lang_code="ru", receive_updates=False, connection_retries=3,
        request_retries=0, flood_sleep_threshold=0, connection=ConnectionTcpObfuscated,
        retry_delay=3)
    client.session.save_entities = False
    try:
        while True:
            try:
                await asyncio.wait_for(client.connect(), 25)
                if not await asyncio.wait_for(client.is_user_authorized(), 15):
                    log("Telegram просит снова запустить setup.py")
                    return 1
                target = await asyncio.wait_for(get_broadcast(client, CHANNEL_USERNAME), 15)
                break
            except (OSError, asyncio.TimeoutError, errors.FloodWaitError) as exc:
                if mode == "check":
                    raise
                log(f"Подключение к Telegram — {describe_error(exc)}; повторим позже")
                try:
                    await asyncio.wait_for(client.disconnect(), 10)
                except (OSError, asyncio.TimeoutError):
                    pass
                await asyncio.sleep(max(20, getattr(exc, "seconds", 0)))
        if target.id != CHANNEL_ID or not target.creator:
            log("Аккаунт не владеет нужным каналом")
            return 1
        log(f"Канал подтверждён — {target.title}")
        if mode == "check":
            await asyncio.gather(preview_sources(client), preview_news(key))
            log("Проверка завершена")
            return 0
        state = State()
        log(f"Автопубликация запущена — версия {VERSION}")
        if not MAX_KEY_FILE.is_file():
            log("MAX — нужен ключ ТелеМакса. Команда: bash start.sh max-key")
        await asyncio.gather(watch_telegram(client, target, state), publish_news(client, target, state, key),
                             watch_max(client, target, state), publish_queue(client, target, state),
                             heartbeat(client, state))
        return 0
    finally:
        await client.disconnect()


def acquire_lock(lock):
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def is_our_worker(pid, project=None):
    proc = Path("/proc") / str(pid)
    project = Path(project).resolve() if project else Path(__file__).resolve().parent
    try:
        if proc.stat().st_uid != os.getuid():
            return False
        args = (proc / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
        return ((proc / "cwd").resolve(strict=True) == project
                and args == [b".venv/bin/python", b"-u", b"worker.py", b"run"])
    except OSError:
        return False


def stop_worker(project=None):
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    with LOCK_FILE.open("a+", encoding="ascii") as lock:
        if acquire_lock(lock):
            log("Сборщик уже остановлен")
            return 0
        lock.seek(0)
        recorded = lock.read(32).strip()
        if recorded.isascii() and recorded.isdecimal() and is_our_worker(int(recorded), project):
            pids = [int(recorded)]
        else:
            # Versions before the restart command did not record their PID.
            pids = [int(proc.name) for proc in Path("/proc").iterdir()
                    if proc.name.isdecimal() and is_our_worker(int(proc.name), project)]
        if len(pids) != 1 or not is_our_worker(pids[0], project):
            log("Не удалось подтвердить процесс сборщика. Повтори restart, когда завершится проверка.")
            return 1
        try:
            os.kill(pids[0], signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = monotonic() + 10
        while monotonic() < deadline:
            if acquire_lock(lock):
                log("Сборщик остановлен")
                return 0
            sleep(0.2)
        log("Сборщик ещё завершает работу")
        return 1


def show_status():
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    with LOCK_FILE.open("a+") as lock:
        running = not acquire_lock(lock)
    log(f"Установлена версия {VERSION}")
    log("Процесс сборщика работает" if running else "Процесс сборщика остановлен")
    if running and HEALTH_FILE.is_file():
        try:
            health = json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
            age = max(0, int((now_utc() - datetime.fromisoformat(health["time"])).total_seconds()))
            log(f"Последний сигнал работы — {age} сек. назад")
            log(f"Источников проверено {health['sources']}, в очереди {health['pending']}")
            if health.get("last_regular"):
                last = datetime.fromtimestamp(health["last_regular"], MSK)
                log(f"Последний обычный пост — {last:%d.%m в %H:%M мск}")
        except (OSError, KeyError, TypeError, ValueError):
            log("Состояние обновится после подключения к Telegram")
    log("Ключ OpenRouter сохранён" if KEY_FILE.is_file() else "Ключ OpenRouter отсутствует")
    boot = Path.home() / ".termux" / "boot" / "che-tam-news"
    log("Скрипт Termux:Boot настроен" if boot.is_file() else "Автозапуск Termux:Boot ещё не настроен")
    return 0 if running else 1


def main():
    parser = argparse.ArgumentParser(description="Автопубликация «Чё там, Чебоксары?»")
    parser.add_argument("mode", choices=["check", "run", "stop", "status"])
    parser.add_argument("--project", help=argparse.SUPPRESS)
    args = parser.parse_args()
    os.umask(0o077)
    logging.getLogger("telethon").setLevel(logging.CRITICAL)
    try:
        if args.mode == "status":
            return show_status()
        if args.mode == "stop":
            return stop_worker(args.project)
        settings = load_settings()
        if not settings.get("phone_session_ready") or not valid_api_pair(settings.get("api_id"),
                                                                         settings.get("api_hash")):
            log("Сначала запусти setup.py для входа в Telegram")
            return 1
        key = KEY_FILE.read_text(encoding="utf-8").strip() if KEY_FILE.exists() else ""
        with LOCK_FILE.open("a+", encoding="ascii") as lock:
            if not acquire_lock(lock):
                log("Сборщик уже запущен")
                return 1
            lock.seek(0)
            lock.truncate()
            if args.mode == "run":
                lock.write(str(os.getpid()))
                lock.flush()
            return asyncio.run(run(args.mode, settings, key))
    except KeyboardInterrupt:
        log("Сборщик остановлен")
        return 0
    except Exception as exc:
        log(f"Сборщик остановлен — {describe_error(exc)}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
