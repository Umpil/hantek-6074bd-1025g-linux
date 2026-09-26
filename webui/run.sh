#!/bin/sh
# Start the web UI in the background. Log: web.log, PID: web.pid.
# Env: PYTHON (interpreter with hantek-linux[web]), PORT (5000), HOST (0.0.0.0), IDLE_RELEASE_S (300).
cd "$(dirname "$0")" || exit 1
if [ -z "$PYTHON" ]; then
    for candidate in ../.venv/bin/python .venv/bin/python python3; do
        if command -v "$candidate" >/dev/null 2>&1; then PYTHON=$candidate; break; fi
    done
fi
if ! "$PYTHON" -c "import flask, hantek_linux" 2>/dev/null; then
    echo "$PYTHON cannot import flask/hantek_linux; run: pip install \".[web]\" from the repository root" >&2
    exit 1
fi
if [ -f web.pid ] && kill -0 "$(cat web.pid)" 2>/dev/null; then
    echo "already running (pid $(cat web.pid)): http://$(hostname -I | awk '{print $1}'):${PORT:-5000}"
    exit 0
fi
nohup "$PYTHON" app.py >> web.log 2>&1 &
echo $! > web.pid
sleep 2
if kill -0 "$(cat web.pid)" 2>/dev/null; then
    echo "running (pid $(cat web.pid)): http://$(hostname -I | awk '{print $1}'):${PORT:-5000}"
else
    echo "failed to start, see web.log:"; tail -n 20 web.log; rm -f web.pid; exit 1
fi
