#!/usr/bin/env bash
# One-shot start of the 4-camera testbed (mediamtx + publishers). The
# publishers run in their own sessions (setsid), so they survive the
# unit's exit.
set -e
cd /home/radxa/falcon/nvr-rk3576
exec ./testbed/start_testbed.sh
