#!/usr/bin/env bash
# Start all 4 cameras through the control panel API once it is serving.
# Retries so it works as a systemd ExecStartPost right after panel boot.
set -u
cd "$(dirname "$0")/.."

attempts=0
while [ "$attempts" -lt 20 ]; do
    if curl -s --max-time 2 http://127.0.0.1:5050/api/cameras > /dev/null 2>&1; then
        for c in cam_a cam_b cam_c cam_d; do
            curl -s -X POST "http://127.0.0.1:5050/api/cameras/$c/start" > /dev/null 2>&1 || true
        done
        echo "cameras started"
        exit 0
    fi
    attempts=$((attempts + 1))
    sleep 1
done
echo "panel did not come up in time; cameras not started" >&2
exit 1
