#!/data/data/com.termux/files/usr/bin/bash
set -eu
umask 077
cd -- "$(dirname -- "$0")"
state_dir="$HOME/.local/share/che-tam"
service_dir="${PREFIX:-/data/data/com.termux/files/usr}/var/service/che-tam-news"

case "${1:-start}" in
  check)
    exec .venv/bin/python -u worker.py check
    ;;
  start|restart)
    if ! command -v sv >/dev/null 2>&1; then
      echo 'Установи службу запуска командой pkg install -y termux-services' >&2
      exit 1
    fi
    mkdir -p "$state_dir" "$service_dir"
    touch "$service_dir/down"
    if [ -e "$service_dir/supervise/control" ]; then
      sv -w 15 down "$service_dir"
    fi
    .venv/bin/python -u worker.py stop
    cat > "$service_dir/run.new" <<'EOF'
#!/data/data/com.termux/files/usr/bin/sh
umask 077
cd "$HOME/che-tam" || exit 1
exec .venv/bin/python -u worker.py run >>"$HOME/.local/share/che-tam/worker.log" 2>&1
EOF
    chmod 700 "$service_dir/run.new"
    mv -f "$service_dir/run.new" "$service_dir/run"
    cat > "$service_dir/finish" <<'EOF'
#!/data/data/com.termux/files/usr/bin/sh
sleep 15
EOF
    chmod 700 "$service_dir/finish"
    termux-wake-lock
    export SVDIR="${PREFIX:-/data/data/com.termux/files/usr}/var/service"
    service-daemon start >/dev/null 2>&1 || true
    for attempt in 1 2 3 4 5 6 7 8 9 10; do
      if [ -e "$service_dir/supervise/control" ]; then
        break
      fi
      sleep 1
    done
    rm -f "$service_dir/down"
    sv -w 15 up "$service_dir"
    echo 'Служба включена. Команда проверки: bash start.sh status'
    ;;
  stop)
    if [ -d "$service_dir" ]; then
      touch "$service_dir/down"
      if [ -e "$service_dir/supervise/control" ]; then
        sv -w 15 down "$service_dir"
      fi
    fi
    exec .venv/bin/python -u worker.py stop
    ;;
  status)
    .venv/bin/python -u worker.py status || true
    if command -v sv >/dev/null 2>&1 && [ -e "$service_dir/supervise/control" ]; then
      sv status "$service_dir" || true
    fi
    tail -n 25 "$state_dir/worker.log" 2>/dev/null || true
    ;;
  max-key)
    exec .venv/bin/python -u max_sources.py
    ;;
  test-alert)
    exec .venv/bin/python -u worker.py test-alert
    ;;
  boot)
    boot_dir="$HOME/.termux/boot"
    mkdir -p "$boot_dir"
    cat > "$boot_dir/che-tam-news" <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
exec bash "$HOME/che-tam/start.sh" start
EOF
    chmod 700 "$boot_dir/che-tam-news"
    echo 'Автозапуск прописан. Установи и один раз открой Termux:Boot из F-Droid.'
    ;;
  log)
    tail -n 40 "$state_dir/worker.log"
    ;;
  *)
    echo 'Использование: bash start.sh check | start | restart | stop | status | boot | log | max-key | test-alert' >&2
    exit 2
    ;;
esac
