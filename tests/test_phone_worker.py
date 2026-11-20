import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import signal
import sys
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


PHONE = Path(__file__).resolve().parents[1] / "phone"
sys.path.insert(0, str(PHONE))
try:
    import fcntl
except ModuleNotFoundError:
    sys.modules["fcntl"] = ModuleType("fcntl")

import worker
from max_sources import select_post
from telethon import errors, functions, types


class StopLoop(Exception):
    pass


class FakeTelegram:
    def __init__(self, now):
        self.channel = types.Channel(id=123, access_hash=456, title="City",
                                     photo=types.ChatPhotoEmpty(), date=now,
                                     broadcast=True, username="pg21ru")
        self.media = types.InputMediaPhoto(types.InputPhoto(10, 20, b"ref"))
        self.message = SimpleNamespace(id=7, date=now, fwd_from=None, noforwards=False,
            message="В Чебоксарах перекроют Калининский мост на время ремонта",
            photo=True, video=None, media=self.media)
        self.sent = []

    async def get_entity(self, handle):
        return self.channel

    async def get_messages(self, channel, *, ids=None, limit=None):
        if limit:
            return [self.message]
        return self.message if ids == self.message.id else None

    async def __call__(self, request):
        self.sent.append(request)


class PhoneWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 11, 21, 9, 0, tzinfo=timezone.utc)
        for field, value in (("STATE_FILE", self.root / "state.json"), ("DATA_DIR", self.root),
                             ("LOCK_FILE", self.root / "worker.lock")):
            patcher = patch.object(worker, field, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(worker, "now_utc", return_value=self.now)
        self.clock = patcher.start()
        self.addCleanup(patcher.stop)
        self.client = FakeTelegram(self.now)
        self.target = types.InputPeerChannel(999, 555)

    async def test_story_survives_throttled_poll_and_restart_then_publishes_once(self):
        state = worker.State()
        source = next(s for s in worker.SOURCES if s.handle == "pg21ru")
        state.sources[source.handle] = {"channel_id": 123, "last_id": 6}
        state.data["last_regular"] = self.now.timestamp()
        await worker.poll_source(self.client, self.target, state, source, self.client.channel)
        self.assertEqual(self.client.sent, [])
        self.assertEqual(state.sources[source.handle]["last_id"], 7)
        state = worker.State()
        self.assertEqual(len(state.pending), 1)
        self.clock.return_value += timedelta(minutes=46)
        with patch.object(worker.asyncio, "sleep", side_effect=StopLoop):
            with self.assertRaises(StopLoop):
                await worker.publish_queue(self.client, self.target, state)
        self.assertEqual(len(self.client.sent), 1)
        self.assertIsInstance(self.client.sent[0], functions.messages.SendMediaRequest)
        self.assertEqual(state.pending, [])
        state = worker.State()
        await worker.poll_source(self.client, self.target, state, source, self.client.channel)
        self.assertEqual(state.pending, [])
        self.assertEqual(len(self.client.sent), 1)

    async def test_unavailable_preview_falls_back_to_text_without_losing_story(self):
        state = worker.State()
        sent = []

        async def reject_preview(request):
            sent.append(request)
            if isinstance(request, functions.messages.SendMediaRequest):
                raise errors.WebpageCurlFailedError(request)

        url = "https://pg21.ru/news/example"
        title = "В Чебоксарах открыли новый каток"
        result = await worker.publish_regular(reject_preview, self.target, state, url, title,
                                             worker.regular_caption(title, url), worker.web_preview(url))
        self.assertTrue(result)
        self.assertEqual(len(sent), 2)
        self.assertIsInstance(sent[1], functions.messages.SendMessageRequest)
        self.assertEqual(sent[0].random_id, sent[1].random_id)
        self.assertEqual(worker.State().recent[-1]["url"], url)

    async def test_reconnect_skips_days_of_history_and_queues_latest_post(self):
        state = worker.State()
        source = next(s for s in worker.SOURCES if s.handle == "pg21ru")
        state.sources[source.handle] = {"channel_id": 123, "last_id": 1}
        self.client.message.id = 20_000
        await worker.poll_source(self.client, self.target, state, source, self.client.channel)
        self.assertEqual(state.sources[source.handle]["last_id"], 20_000)
        self.assertEqual(state.pending[0]["message_id"], 20_000)

    async def test_read_only_status_does_not_need_login_settings(self):
        with patch.object(worker, "acquire_lock", return_value=True), \
                patch.object(worker, "load_settings", side_effect=AssertionError("Must not open login settings")):
            self.assertEqual(worker.show_status(), 1)

    async def test_max_accepts_only_fresh_alerts_from_the_official_sources(self):
        handle = "id2127305504_gos"
        url = f"https://max.ru/{handle}/alert"
        alert = {"type": "monitoring.hit.created", "data": {
            "source": {"platform": "max", "username": handle},
            "message": {"text": "В Чувашской Республике объявлена опасность атаки БПЛА!",
                        "link": url,
                        "published_at": (self.now - timedelta(minutes=9)).isoformat()}}}
        enabled = self.now - timedelta(hours=1)
        post = select_post(alert, self.now, enabled)
        self.assertEqual((post.signal, post.phase), ("official:drone", "тревога"))
        self.assertIsNone(select_post(alert, self.now + timedelta(minutes=4), enabled))
        alert["data"]["source"]["username"] = "channel_radar_russia_bpla"
        alert["data"]["message"]["link"] = "https://max.ru/channel_radar_russia_bpla/alert"
        self.assertIsNone(select_post(alert, self.now, enabled))

    async def test_delayed_max_alert_cannot_override_newer_all_clear(self):
        state = worker.State()
        state.signals["official:drone"] = {"time": self.now.timestamp(), "phase": "отбой"}
        published = self.now - timedelta(minutes=3)
        result = await worker.publish_alert(self.client, self.target, state, "official:drone",
                                            "тревога", published, "https://max.ru/example/alert", "alert")
        self.assertFalse(result)
        self.assertEqual(self.client.sent, [])

    async def test_max_recognizes_a_news_headline_after_the_ministers_byline(self):
        handle = "id2128054356_gos"
        event = {"type": "monitoring.hit.created", "data": {
            "source": {"platform": "max", "username": handle},
            "message": {"text": "Дмитрий Захаров:\nВ Чебоксарах открыли новую школу.",
                        "link": f"https://max.ru/{handle}/school",
                        "published_at": self.now.isoformat()}}}
        post = select_post(event, self.now, self.now - timedelta(minutes=1))
        self.assertEqual(post.title, "В Чебоксарах открыли новую школу.")
        self.assertIsNone(post.signal)

    async def test_neighbor_alerts_keep_separate_regions_and_skip_speculation(self):
        def selected(handle, body):
            event = {"type": "monitoring.hit.created", "data": {
                "source": {"platform": "max", "username": handle},
                "message": {"text": body, "link": f"https://max.ru/{handle}/example",
                            "published_at": self.now.isoformat()}}}
            return select_post(event, self.now, self.now - timedelta(minutes=1))

        tatarstan = selected("id1660025250_gos", "Внимание! В Татарстане введён режим «Беспилотная опасность».")
        mordovia = selected("officialmordovia", "В Мордовии отбой сигнала «Беспилотная опасность».")
        nizhny = selected("id5260494537_gos", "Режим ракетной опасности на территории Нижегородской области объявлен.")
        self.assertEqual((tatarstan.signal, tatarstan.phase), ("official:tatarstan:drone", "тревога"))
        self.assertEqual((mordovia.signal, mordovia.phase), ("official:mordovia:drone", "отбой"))
        self.assertEqual(nizhny.signal, "official:nizhny-novgorod:rocket")
        self.assertIn("Соседние регионы · Татарстан", tatarstan.title)
        self.assertIsNone(selected("id1660025250_gos", "В Чувашии объявили опасность атаки БПЛА."))
        self.assertIsNone(selected("officialmordovia", "В Мордовии возможна ракетная опасность."))
        self.assertIsNone(selected("officialmordovia", "В Мордовии объявлена учебная ракетная опасность."))
        state = worker.State()
        state.signals["official:drone"] = {"time": self.now.timestamp(), "phase": "тревога"}
        self.assertTrue(await worker.publish_alert(self.client, self.target, state, mordovia.signal,
                                                    mordovia.phase, mordovia.published, mordovia.url,
                                                    mordovia.title))
        self.assertEqual(state.signals["official:drone"]["phase"], "тревога")

    async def test_synthetic_alert_goes_only_to_saved_messages(self):
        await worker.send_test_alert(self.client)
        self.assertEqual(len(self.client.sent), 1)
        sent = self.client.sent[0]
        self.assertIsInstance(sent.peer, types.InputPeerSelf)
        self.assertTrue(sent.message.startswith("🧪 ТЕСТ"))
        self.assertIn("учебный пример", sent.message)
        self.assertIn("В Чувашии объявлена опасность атаки БПЛА", sent.message)
        self.assertFalse(worker.STATE_FILE.exists())

    async def test_stop_checks_project_and_never_kills_an_unrelated_process(self):
        worker.LOCK_FILE.write_text("444", encoding="ascii")
        with patch.object(worker, "acquire_lock", side_effect=[False, True]), \
                patch.object(worker, "is_our_worker", return_value=True) as check, \
                patch.object(worker.os, "kill") as kill:
            self.assertEqual(worker.stop_worker("/existing/project"), 0)
            check.assert_called_with(444, "/existing/project")
            kill.assert_called_once_with(444, signal.SIGTERM)
        fake_proc = SimpleNamespace(iterdir=lambda: [])
        with patch.object(worker, "acquire_lock", return_value=False), \
                patch.object(worker, "is_our_worker", return_value=False), \
                patch.object(worker, "Path", return_value=fake_proc), patch.object(worker.os, "kill") as kill:
            self.assertEqual(worker.stop_worker("/existing/project"), 1)
            kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
