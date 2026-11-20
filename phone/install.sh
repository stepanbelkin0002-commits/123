#!/data/data/com.termux/files/usr/bin/bash
set -eu
umask 077
project="$HOME/che-tam"
mkdir -p "$project"
cd "$project"

if [ ! -x .venv/bin/python ]; then
  python -m venv .venv
fi
staging=$(mktemp -d "$project/.update.XXXXXXXX")
trap 'rm -rf -- "$staging"' EXIT
curl -fL --retry 2 --connect-timeout 15 --max-time 120 \
  'https://codeload.github.com/stepanbelkin0002-commits/123/zip/refs/heads/termux-worker' \
  -o "$staging/source.zip"

.venv/bin/python - "$staging" <<'PY'
from pathlib import Path, PurePosixPath
import sys
from zipfile import ZipFile

stage = Path(sys.argv[1])
names = ("worker.py", "news.py", "news_media.py", "max_sources.py", "telegram_sources.py",
         "setup.py", "doctor.py", "requirements.txt", "start.sh", "install.sh")
with ZipFile(stage / "source.zip") as archive:
    for name in names:
        matches = [member for member in archive.namelist()
                   if len(PurePosixPath(member).parts) == 3
                   and PurePosixPath(member).parts[-2:] == ("phone", name)]
        if len(matches) != 1:
            raise SystemExit("В обновлении отсутствует файл " + name)
        data = archive.read(matches[0]).replace(b"\r\n", b"\n")
        if name.endswith(".py"):
            compile(data, name, "exec")
        (stage / name).write_bytes(data)
print("Файлы обновления проверены.")
PY

if ! .venv/bin/python -c 'import telethon' >/dev/null 2>&1; then
  .venv/bin/python -m pip install -r "$staging/requirements.txt"
fi
if ! command -v sv >/dev/null 2>&1; then
  pkg install -y termux-services
fi
service_dir="${PREFIX:-/data/data/com.termux/files/usr}/var/service/che-tam-news"
if [ -e "$service_dir/supervise/control" ]; then
  touch "$service_dir/down"
  sv -w 30 stop "$service_dir"
fi
.venv/bin/python "$staging/worker.py" stop --project "$project"

.venv/bin/python - "$staging" "$project" <<'PY'
from pathlib import Path
import sys

stage, project = map(Path, sys.argv[1:])
for source in stage.iterdir():
    if source.is_file() and source.suffix in (".py", ".sh", ".txt"):
        temporary = project / (source.name + ".new")
        temporary.write_bytes(source.read_bytes())
        temporary.chmod(0o700 if source.suffix == ".sh" else 0o600)
        temporary.replace(project / source.name)
PY
bash start.sh boot
bash start.sh start
sleep 8
bash start.sh status
