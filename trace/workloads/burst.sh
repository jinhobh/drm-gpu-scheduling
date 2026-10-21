#!/usr/bin/env bash
# Attribution workload: burst_client (the ground-truth victim) with one background hog.
#
#   trace/workloads/burst.sh HOG [BURST_CLIENT_ARGS...]
#   sudo /home/jinho/Projects/drm-gpu-scheduling/trace/capture.sh burst-HOG -- \
#       /home/jinho/Projects/drm-gpu-scheduling/trace/workloads/burst.sh HOG --iters 736 -b 40
#
# HOG is a trace/workloads/hog.sh name. The hog warms up for HOG_WARMUP seconds (3) before
# the first burst. burst_client's log goes to $CAPTURE_OUT/burst.csv (set by capture.sh),
# else ./burst.csv; the vk-hog's own frame log goes next to it as hog.csv.
# The run happens on an empty Hyprland workspace (switched back at exit), so the layout is
# the same every run: a tiled hog (glmark2, vkmark) fills the workspace alone, and the
# burst_client windows (victim, vk-hog) float at a fixed size.
set -u
here=$(cd "$(dirname "$0")" && pwd)
hog=${1:?usage: burst.sh HOG [BURST_CLIENT_ARGS...]}
shift
out=${CAPTURE_OUT:-.}

pid=""
bc=""
ws=$(hyprctl activeworkspace -j | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
hyprctl dispatch 'hl.dsp.focus({ workspace = "empty" })' > /dev/null
echo "burst.sh: workspace $ws -> $(hyprctl activeworkspace -j | python3 -c 'import json,sys
w = json.load(sys.stdin); print(w["id"], "with", w["windows"], "windows")')"
# supertuxkart ignores SIGINT: escalate to TERM, then KILL
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
# capture.sh's timeout signals only this shell: pass it on so burst_client writes its log
trap '[[ -n $bc ]] && kill -INT "$bc" 2>/dev/null' INT TERM

# GPU timestamp correction, measured while the GPU is still idle (cached for burst_client)
"$here/../burst_client/burst_client" --probe || echo "burst.sh: warning: loose probe" >&2
if [[ $hog != none ]]; then
    HOG_LOG="$out/hog.csv" "$here/hog.sh" "$hog" &
    pid=$!
    sleep "${HOG_WARMUP:-3}"
    kill -0 "$pid" 2>/dev/null || { echo "burst.sh: hog $hog exited during warmup" >&2; exit 1; }
fi
echo "burst.sh: hog=$hog burst_client $*"
"$here/../burst_client/burst_client" -o "$out/burst.csv" "$@" &
bc=$!
while kill -0 "$bc" 2>/dev/null; do wait "$bc"; done
