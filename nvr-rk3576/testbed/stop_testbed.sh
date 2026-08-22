#!/usr/bin/env bash
# Stop one camera's publisher (./stop_testbed.sh cam_b) or the whole
# testbed (./stop_testbed.sh all). Kills the publisher's process group
# so respawn loops and ffmpeg children all die.
set -u
cd "$(dirname "$0")"

usage() {
    echo "usage: $0 {cam_a|cam_b|cam_c|cam_d|all}"
    exit 1
}

[ $# -eq 1 ] || usage

stop_group() {
    local name=$1 pidfile=$2
    if [ ! -f "$pidfile" ]; then
        echo "$name: no pid file ($pidfile), nothing to do"
        return 0
    fi
    local pid
    pid=$(cat "$pidfile")
    if kill -0 "$pid" 2>/dev/null; then
        kill -- "-$pid" 2>/dev/null && echo "$name: killed group $pid"
    else
        echo "$name: process group $pid not running"
    fi
    rm -f "$pidfile"
}

case "$1" in
    cam_a|cam_b|cam_c|cam_d)
        stop_group "$1" "$1.pid"
        ;;
    all)
        for name in cam_a cam_b cam_c cam_d; do
            stop_group "$name" "$name.pid"
        done
        if [ -f mediamtx.pid ]; then
            pid=$(cat mediamtx.pid)
            if kill -0 "$pid" 2>/dev/null; then
                kill "$pid" 2>/dev/null && echo "mediamtx: killed $pid"
            else
                echo "mediamtx: not running"
            fi
            rm -f mediamtx.pid
        fi
        ;;
    *)
        usage
        ;;
esac
