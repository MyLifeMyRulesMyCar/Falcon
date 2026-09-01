#!/usr/bin/env bash
# Start all 4 cameras through the control panel API once it is serving.
# Retries so it works as a systemd ExecStartPost right after panel boot.
#
# v1.6: honours the panel's TLS + Basic Auth.
#   - uses https + the panel's own cert when config/panel.crt exists
#   - authenticates via FALCON_PANEL_AUTH="user:pass" when the panel is hardened
set -u
cd "$(dirname "$0")/.."

SCHEME="http"
CURL_EXTRA=()
if [ -f config/panel.crt ]; then
    SCHEME="https"
    CURL_EXTRA+=(--cacert config/panel.crt)
fi
if [ -n "${FALCON_PANEL_AUTH:-}" ]; then
    CURL_EXTRA+=(-u "$FALCON_PANEL_AUTH")
fi
BASE="$SCHEME://127.0.0.1:5050"

attempts=0
while [ "$attempts" -lt 20 ]; do
    if curl -s --max-time 2 "${CURL_EXTRA[@]}" "$BASE/api/cameras" > /dev/null 2>&1; then
        for c in cam_a cam_b cam_c cam_d; do
            curl -s "${CURL_EXTRA[@]}" -X POST "$BASE/api/cameras/$c/start" > /dev/null 2>&1 || true
        done
        echo "cameras started"
        exit 0
    fi
    attempts=$((attempts + 1))
    sleep 1
done
echo "panel did not come up in time; cameras not started" >&2
exit 1
