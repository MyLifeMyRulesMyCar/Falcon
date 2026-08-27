#!/usr/bin/env bash
# One-time environment install for the Falcon NVR (RK3576). Idempotent —
# safe to re-run; every step skips work that's already done.
#
#   ./scripts/install.sh
#
# After a reboot, bring the whole stack up with:
#   ./scripts/start_all.sh
set -e
cd "$(dirname "$0")/.."          # -> nvr-rk3576 (project dir)
PROJ="$(pwd)"
ROOT="$(dirname "$PROJ")"        # repo root (holds .venv)
VENV="$ROOT/.venv"

# 1. lowercase /home/radxa/falcon -> actual repo root (docs + systemd units
#    hardcode the lowercase path).
if [ ! -e /home/radxa/falcon ] && [ -d "$ROOT" ]; then
    ln -s "$ROOT" /home/radxa/falcon
    echo "symlinked /home/radxa/falcon -> $ROOT"
fi

# 2. Python venv at the repo root (matches scripts/run_panel.sh + systemd units).
if [ ! -x "$VENV/bin/python" ]; then
    echo "creating venv at $VENV ..."
    python3 -m venv "$VENV"
fi

# 3. Python deps. rknn-toolkit-lite2 is PyPI-only per arch, so it's pinned
#    here rather than in requirements.txt.
echo "installing python dependencies ..."
"$VENV/bin/pip" install -r "$PROJ/requirements.txt" "rknn-toolkit-lite2==2.3.2"

# 4. ffmpeg must have rkmpp decoders + https/tls (built per
#    docs/ffmpeg_rebuild_step1.md). Warn, don't fail — the rest installs fine.
if ! ffmpeg -decoders 2>/dev/null | grep -q h264_rkmpp || \
   ! ffmpeg -protocols 2>/dev/null | grep -q https; then
    echo "WARNING: ffmpeg is missing rkmpp/HTTPS support." >&2
    echo "  Rebuild it per docs/ffmpeg_rebuild_step1.md, or install a distro build with rkmpp." >&2
fi

# 5. NPU model (gitignored). The Radxa demo tarball also gives bus.jpg, which
#    scripts/verify_detector_m2.py reads for the calibration gate.
MODEL="$PROJ/nvr/inference/model/yolov5s_relu_rk3576.rknn"
if [ ! -f "$MODEL" ]; then
    echo "fetching NPU model from the Radxa YOLOv5 demo ..."
    curl -sL -o /tmp/rk3576_rknn_yolov5_demo.tar.gz \
        https://dl.radxa.com/rock4/4d/images/rk3576_rknn_yolov5_demo.tar.gz
    tar -xzf /tmp/rk3576_rknn_yolov5_demo.tar.gz -C "$HOME"
    mkdir -p "$(dirname "$MODEL")"
    cp "$HOME/rk3576_rknn_yolov5_demo/model/yolov5s_relu_rk3576.rknn" "$MODEL"
fi

# 6. mediamtx binary for the testbed (pinned v1.12.2 — newer releases break
#    HLS on the looped testbed clip). Primary mirror may stall on this board,
#    so fall back to the GitHub release directly.
if [ ! -x "$PROJ/testbed/mediamtx" ]; then
    echo "downloading mediamtx v1.12.2 ..."
    curl -fL --http1.1 -o /tmp/mediamtx.tar.gz \
        "https://ghproxy.net/https://github.com/bluenviron/mediamtx/releases/download/v1.12.2/mediamtx_v1.12.2_linux_arm64.tar.gz" \
        || curl -fL -o /tmp/mediamtx.tar.gz \
        "https://github.com/bluenviron/mediamtx/releases/download/v1.12.2/mediamtx_v1.12.2_linux_arm64.tar.gz"
    tar -xzf /tmp/mediamtx.tar.gz -C /tmp mediamtx
    cp /tmp/mediamtx "$PROJ/testbed/mediamtx"
    chmod +x "$PROJ/testbed/mediamtx"
fi

# 7. mosquitto (local MQTT broker for M5). Best-effort — needs sudo.
if ! command -v mosquitto >/dev/null 2>&1 && ! [ -x /usr/sbin/mosquitto ]; then
    echo "installing mosquitto (local MQTT broker) ..."
    sudo apt-get install -y mosquitto mosquitto-clients \
        || echo "WARNING: could not install mosquitto (needs sudo). MQTT output will be unavailable." >&2
fi

echo
echo "install complete."
echo "  After a reboot, run:   $PROJ/scripts/start_all.sh"
echo "  Panel:                 http://<board-ip>:5050"
echo "  NPU gate (needs NPU):  $VENV/bin/python $PROJ/scripts/verify_detector_m2.py"
echo "  Unit tests:            $VENV/bin/python -m pytest $PROJ/tests/ -q"
