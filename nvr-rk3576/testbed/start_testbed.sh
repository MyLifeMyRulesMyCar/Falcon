#!/usr/bin/env bash
# Start the M1 4-camera testbed: mediamtx + 4 independent publishers,
# one per camera. Every publisher runs in its own process group (setsid)
# with a per-camera PID file so any one can be killed without touching
# the others (see stop_testbed.sh).
set -e
cd "$(dirname "$0")"

# Generate the shared loopable test clip on first run: the first 30s of the
# Big Buck Bunny trailer (real content — people/animals for the NPU), scaled
# to 640x360 and re-encoded with h264_rkmpp (this board's ffmpeg build has no
# libx264 encoder). Two steps (download then transcode) so a flaky network
# can't break the encode.
if [ ! -f sample.mp4 ]; then
    echo "generating testbed/sample.mp4 (BBB trailer 30s, h264_rkmpp) ..."
    curl -sL -o .bbb_src.mp4 \
        https://uploads.video-commander.com/sample/BigBuckBunny.mp4
    ffmpeg -loglevel error -i .bbb_src.mp4 -t 30 \
        -vf scale=640:360 -c:v h264_rkmpp -b:v 800k -g 30 -r 30 sample.mp4
    rm -f .bbb_src.mp4
fi

if [ -f mediamtx.pid ] && kill -0 "$(cat mediamtx.pid)" 2>/dev/null; then
    echo "mediamtx already running (pid $(cat mediamtx.pid))"
    exit 1
fi

setsid ./mediamtx mediamtx.yml > mediamtx.log 2>&1 &
echo $! > mediamtx.pid
sleep 3

# cam_a: RTSP publish, read back over RTSP
# -rtsp_transport tcp: mediamtx's HLS muxer crashes ("too many reordered
# frames") when loopback UDP RTP reorders under load (soak finding) — TCP is
# ordered/loss-free even under load.
setsid ffmpeg -loglevel error -re -stream_loop -1 -i sample.mp4 \
    -c copy -rtsp_transport tcp -f rtsp rtsp://127.0.0.1:8554/cam_a \
    > cam_a.log 2>&1 &
echo $! > cam_a.pid

# cam_b: RTMP publish, read back over RTMP
setsid ffmpeg -loglevel error -re -stream_loop -1 -i sample.mp4 \
    -c copy -f flv rtmp://127.0.0.1:1935/cam_b \
    > cam_b.log 2>&1 &
echo $! > cam_b.pid

# cam_c: RTSP publish, read back over HTTP (HLS on :8888)
setsid ffmpeg -loglevel error -re -stream_loop -1 -i sample.mp4 \
    -c copy -rtsp_transport tcp -f rtsp rtsp://127.0.0.1:8554/cam_c \
    > cam_c.log 2>&1 &
echo $! > cam_c.pid

# cam_d: HTTP-FLV server (ffmpeg -listen 1) + RTSP republish to mediamtx so
# the web player works at http://host:8888/cam_d/. The listen server serves a
# single client and exits when it disconnects; a client that dies mid-header
# can leave it connected-but-broken, so a watchdog restarts it while a client
# is attached during the first 60s of the server's life (broken slots only
# ever occur at spawn/startup churn; a long-lived healthy connection is
# never touched). The whole group is killed via the PID file.
setsid sh -c '
    PUB_START=$(date +%s); \
    ffmpeg -loglevel error -re -stream_loop -1 -i sample.mp4 \
        -c copy -rtsp_transport tcp -f rtsp rtsp://127.0.0.1:8554/cam_d \
        > /dev/null 2>&1 &
    while true; do
        ffmpeg -loglevel error -re -stream_loop -1 -i sample.mp4 \
            -c copy -f flv -listen 1 http://127.0.0.1:8080/cam_d.flv & \
        SRV=$!; \
        START=$(date +%s); \
        while kill -0 $SRV 2>/dev/null; do \
            NOW=$(date +%s); \
            # Watchdog only during the publisher group'\''s first 60s (spawn/
            # startup churn is where broken slots appear); a long-lived healthy
            # connection in steady state is never touched.
            if [ $(( NOW - PUB_START )) -lt 60 ] && \
               [ $(( NOW - START )) -ge 20 ] && \
               ss -tn 2>/dev/null | grep -q "ESTAB.*127.0.0.1:8080"; then \
                kill $SRV 2>/dev/null; \
            fi; \
            sleep 2; \
        done; \
        sleep 1; \
    done' > cam_d.log 2>&1 &
echo $! > cam_d.pid

# Verify all four sources are actually readable before declaring success.
# Retry: publishers take a moment to complete their handshake with mediamtx.
# cam_d is verified via its RTSP republish: the flv listen server on :8080
# exits whenever a client (like this probe) disconnects, so probing it here
# would just churn it; the ingest worker's own retry handles that path.
fail=0
for entry in \
    "cam_a rtsp://127.0.0.1:8554/cam_a" \
    "cam_b rtmp://127.0.0.1:1935/cam_b" \
    "cam_c http://127.0.0.1:8888/cam_c/index.m3u8" \
    "cam_d rtsp://127.0.0.1:8554/cam_d"; do
    set -- $entry
    ok=0
    for attempt in 1 2 3 4 5; do
        if ffprobe -v error -select_streams v:0 -show_entries stream=width,height \
            -of csv=p=0 "$2" > /dev/null 2>&1; then
            ok=1
            break
        fi
        sleep 2
    done
    if [ "$ok" -eq 1 ]; then
        echo "OK  $1 ($2)"
    else
        echo "FAIL $1 ($2)"
        fail=1
    fi
done

if [ "$fail" -ne 0 ]; then
    echo "testbed verification failed; see testbed/*.log"
    exit 1
fi
echo "testbed ready"
