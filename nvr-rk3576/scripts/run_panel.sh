#!/usr/bin/env bash
# Detached launcher for the control panel (M1.1 + M2.1 detection).
# Logs to nvr-rk3576/panel.log (gitignored). Run with:
#   nohup setsid scripts/run_panel.sh < /dev/null > /dev/null 2>&1 &
cd "$(dirname "$0")/.."
PY=${PYTHON:-../.venv/bin/python}
exec "$PY" scripts/run_control_panel.py --host 0.0.0.0 >> panel.log 2>&1
