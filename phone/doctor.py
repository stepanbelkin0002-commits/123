"""Check the phone's runtime and network without printing account secrets."""

from datetime import datetime, timedelta, timezone
import fcntl
import json
from pathlib import Path
import socket
import sqlite3
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener


DATA = Path.home() / ".local" / "share" / "che-tam"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    print(datetime.now(timezone(timedelta(hours=3))).strftime("Проверка на POCO — %d.%m.%Y %H:%M мск"), flush=True)
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (DATA / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            print("Сборщик сейчас остановлен", flush=True)
        except BlockingIOError:
            print("Процесс сборщика сейчас работает", flush=True)
    session = DATA / "phone_account.session"
    try:
        with sqlite3.connect(session.resolve().as_uri() + "?mode=ro", uri=True, timeout=2) as connection:
            row = connection.execute("SELECT server_address, port FROM sessions LIMIT 1").fetchone()
        if not row:
            print("В сохранённой сессии нет адреса Telegram", flush=True)
        else:
            with socket.create_connection((row[0], row[1]), timeout=10):
                print("TCP-подключение к серверу Telegram доступно", flush=True)
    except (OSError, sqlite3.Error) as exc:
        print(f"Подключение к серверу Telegram — {type(exc).__name__}", flush=True)
    keyfile = DATA / "openrouter.key"
    if not keyfile.is_file() or not keyfile.stat().st_size:
        print("Ключ OpenRouter на телефоне отсутствует", flush=True)
    else:
        request = Request("https://openrouter.ai/api/v1/key",
                          headers={"Authorization": "Bearer " + keyfile.read_text().strip()})
        try:
            with build_opener(NoRedirect()).open(request, timeout=12) as response:
                payload = json.load(response)
            if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                print("OpenRouter доступен, сохранённый ключ принят", flush=True)
            else:
                print("OpenRouter прислал ответ неизвестного формата", flush=True)
        except HTTPError as exc:
            print(f"OpenRouter — HTTP {exc.code}", flush=True)
        except (OSError, ValueError) as exc:
            print(f"OpenRouter — {type(exc).__name__}", flush=True)
    log = DATA / "worker.log"
    if log.is_file():
        with log.open("rb") as stream:
            stream.seek(max(0, log.stat().st_size - 16_000))
            lines = stream.read().decode("utf-8", errors="replace").splitlines()
        print("Последние сообщения журнала", flush=True)
        print("\n".join(lines[-12:]), flush=True)


if __name__ == "__main__":
    main()
