#!/usr/bin/env bash
# Bring the whole NVR stack up after a reboot. Manual (no systemd).
# Idempotent — safe to re-run; already-running parts are skipped.
#
#   ./scripts/start_all.sh
#
# Stops nothing; to tear down:
#   ./testbed/stop_testbed.sh all
#   pkill -f run_control_panel
set -e
cd "$(dirname "$0")/.."          # -> nvr-rk3576 (project dir)
PROJ="$(pwd)"
ROOT="$(dirname "$PROJ")"        # repo root

# 1. testbed: mediamtx + 4 publishers. Generates testbed/sample.mp4 on first
#    run (downloads the BBB clip, encodes with h264_rkmpp).
if ! pgrep -f '[m]ediamtx' > /dev/null; then
    echo "[1/4] starting testbed ..."
    "$PROJ/testbed/start_testbed.sh"
else
    echo "[1/4] testbed already running"
fi

# 2. control panel (ingest + detection + zones + MQTT/HTTP + previews),
#    detached, bound to 0.0.0.0:5050.
if ! pgrep -f '[r]un_control_panel' > /dev/null; then
    echo "[2/4] starting control panel on 0.0.0.0:5050 ..."
    setsid nohup "$PROJ/scripts/run_panel.sh" < /dev/null > /dev/null 2>&1 &
    sleep 12
else
    echo "[2/4] panel already running"
fi

# 3. cameras through the panel API (they begin stopped). v1.6: use https +
#    the panel's own cert when hardened, and authenticate via
#    FALCON_PANEL_AUTH="user:pass".
SCHEME="http"
CURL_EXTRA=()
if [ -f config/panel.crt ]; then
    SCHEME="https"
    CURL_EXTRA+=(--cacert config/panel.crt)
fi
if [ -n "${FALCON_PANEL_AUTH:-}" ]; then
    CURL_EXTRA+=(-u "$FALCON_PANEL_AUTH")
fi
echo "[3/4] starting cameras ..."
for c in cam_a cam_b cam_c cam_d; do
    curl -s "${CURL_EXTRA[@]}" -X POST "$SCHEME://127.0.0.1:5050/api/cameras/$c/start" > /dev/null || true
done

# 4. mosquitto (M5 broker) — it is a systemd service, so this is just a
#    belt-and-braces ensure-running.
if [ -x /usr/sbin/mosquitto ] && ! pgrep -f '[m]osquitto' > /dev/null; then
    echo "[4/4] starting mosquitto ..."
    sudo -n systemctl restart mosquitto 2>/dev/null || true
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "stack is up:"
echo "  panel    http://127.0.0.1:5050        (LAN: http://$IP:5050)"
echo "  testbed  RTSP/RTMP/HLS/HTTP-FLV on 127.0.0.1 (mediamtx)"
echo "  mqtt     mosquitto_sub -h 127.0.0.1 -p 1883 -t 'nvr/#'"
echo "  logs     nvr-rk3576/panel.log, nvr-rk3576/testbed/*.log"
