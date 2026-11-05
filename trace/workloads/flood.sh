#!/usr/bin/env bash
# Throughput-only cell: one background load alone, no victim, for a fixed time.
#
#   trace/workloads/flood.sh HOG SECONDS
#   sudo /home/jinho/Projects/drm-gpu-scheduling/trace/capture.sh flood -- \
#       /home/jinho/Projects/drm-gpu-scheduling/trace/workloads/flood.sh vkmark-cube 20
#
# Runs on an empty workspace like burst.sh, so the window size (and job cost) does not depend
# on the user's layout. With `vkmark-cube` (mailbox present) the rate is set by GPU/CPU
# submission, not by the display refresh; check that in pilots. Measure the rate from the
# trace with `python3 trace/throughput.py RUN`. HOG is a trace/workloads/hog.sh name.
set -u
here=$(cd "$(dirname "$0")" && pwd)
hog=${1:?usage: flood.sh HOG SECONDS}
secs=${2:?usage: flood.sh HOG SECONDS}
pid=""

ws=$(hyprctl activeworkspace -j | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
hyprctl dispatch 'hl.dsp.focus({ workspace = "empty" })' > /dev/null
echo "flood.sh: workspace $ws -> $(hyprctl activeworkspace -j | python3 -c 'import json,sys
w = json.load(sys.stdin); print(w["id"], "with", w["windows"], "windows")')"
cleanup() {
    [[ -n $ws ]] && hyprctl dispatch "hl.dsp.focus({ workspace = $ws })" > /dev/null
    [[ -n $pid ]] || return
    for sig in INT TERM KILL; do
        kill -$sig "$pid" 2>/dev/null || break
        for _ in $(seq 30); do kill -0 "$pid" 2>/dev/null || break 2; sleep 0.1; done
    done
    wait 2>/dev/null
}
trap cleanup EXIT
trap 'exit 130' INT TERM

echo "flood.sh: hog=$hog seconds=$secs"
HOG_LOG="${CAPTURE_OUT:-.}/hog.csv" "$here/hog.sh" "$hog" &
pid=$!
sleep "$secs"
kill -0 "$pid" 2>/dev/null || { echo "flood.sh: hog $hog exited early" >&2; exit 1; }
