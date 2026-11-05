#!/usr/bin/env bash
# Two independent NORMAL interactive victims, no hog (self-cost cells of the kernel protocol).
#
#   trace/workloads/two_victims.sh aligned|offset [BURST_CLIENT_ARGS...]
#   sudo /home/jinho/Projects/drm-gpu-scheduling/trace/capture.sh two-offset -- \
#       /home/jinho/Projects/drm-gpu-scheduling/trace/workloads/two_victims.sh offset \
#       --iters 736 -n 10 -b 20 --jobs 3
#
# Two burst_client processes (separate DRM clients, so separate scheduler entities) tile side
# by side on an empty workspace (--tile; A opens first, so A is left), both fully visible.
# Bursts follow one shared CLOCK_MONOTONIC grid (--grid-ms GRID_MS, default 800, from a
# common --start-at): `aligned` starts both clients' bursts together, `offset` starts B half a
# refresh later. Later frames of a burst follow each client's frame callbacks, which come
# from the same output, so only burst starts are offset. Logs: $CAPTURE_OUT/burst-a.csv and
# burst-b.csv. Join each with `trace/join.py RUN --log burst-a.csv` (and burst-b.csv).
set -u
here=$(cd "$(dirname "$0")" && pwd)
mode=${1:?usage: two_victims.sh aligned|offset [BURST_CLIENT_ARGS...]}
shift
[[ $mode == aligned || $mode == offset ]] || { echo "two_victims.sh: bad mode $mode" >&2; exit 2; }
out=${CAPTURE_OUT:-.}
bc=../burst_client/burst_client
a="" b=""

ws=$(hyprctl activeworkspace -j | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
hyprctl dispatch 'hl.dsp.focus({ workspace = "empty" })' > /dev/null
echo "two_victims.sh: workspace $ws -> $(hyprctl activeworkspace -j | python3 -c 'import json,sys
w = json.load(sys.stdin); print(w["id"], "with", w["windows"], "windows")')"
cleanup() {
    [[ -n $ws ]] && hyprctl dispatch "hl.dsp.focus({ workspace = $ws })" > /dev/null
    for p in $a $b; do kill -INT "$p" 2>/dev/null; done
    wait 2>/dev/null
}
trap cleanup EXIT
# capture.sh's timeout signals only this shell: pass it on so both clients write their logs
trap 'for p in $a $b; do kill -INT "$p" 2>/dev/null; done' INT TERM

# half a refresh of the focused output, for the offset
refresh_hz=$(hyprctl monitors -j | python3 -c 'import json,sys
print(next(m["refreshRate"] for m in json.load(sys.stdin) if m["focused"]))')
half_us=$(python3 -c "print(round(1e6 / $refresh_hz / 2))")
offset=0
[[ $mode == offset ]] && offset=$half_us

"$here/$bc" --probe || echo "two_victims.sh: warning: loose probe" >&2
start=$(python3 -c 'import time; print(time.clock_gettime_ns(time.CLOCK_MONOTONIC) + 4_000_000_000)')
grid=${GRID_MS:-800}
echo "two_victims.sh: mode=$mode refresh=${refresh_hz}Hz offset_us=$offset grid_ms=$grid start_at=$start burst_client $*"
"$here/$bc" --tile --grid-ms "$grid" --start-at "$start" -o "$out/burst-a.csv" "$@" &
a=$!
sleep 0.5  # deterministic tiling order: A left, B right
"$here/$bc" --tile --grid-ms "$grid" --start-at "$start" --offset-us "$offset" \
    -o "$out/burst-b.csv" "$@" &
b=$!
status=0
for p in $a $b; do
    # a trapped signal interrupts wait (status > 128) while the client is still writing its log
    while :; do
        wait "$p"; rc=$?
        kill -0 "$p" 2>/dev/null || break
    done
    [[ $rc -eq 0 ]] || status=1
done
a="" b=""
exit $status
