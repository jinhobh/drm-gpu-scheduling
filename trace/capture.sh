#!/usr/bin/env bash
# Record a drm_sched / amdgpu trace while a workload runs as the invoking user.
#
#   sudo trace/capture.sh NAME [SECONDS] -- WORKLOAD [ARGS...]
#
# Output: traces/NAME/<timestamp>/ (owned by the invoking user)
#   trace.dat     trace-cmd record, CLOCK_MONOTONIC timestamps (-C mono)
#   bpf.txt       per-job pid/comm/priority and fence deadlines (trace/bpf/ids.bt)
#   clients.jsonl DRM clients from /proc/*/fdinfo, sampled every 0.5 s
#   gpu_metrics.csv  clocks / activity / power at every SMU table refresh (~6 ms)
#   meta.txt      kernel, module parameters, DPM state, monitors
#   workload.log  the workload's stdout/stderr
# The workload runs as $SUDO_USER inside the Hyprland session. If SECONDS is given it is
# stopped after that long; otherwise the capture ends when the workload exits.
set -euo pipefail

die() { echo "capture: $*" >&2; exit 1; }
[[ $EUID -eq 0 && -n "${SUDO_USER:-}" ]] || die "run with sudo from your session"
[[ $# -ge 3 ]] || die "usage: sudo trace/capture.sh NAME [SECONDS] -- WORKLOAD [ARGS...]"

name=$1; shift
secs=""
if [[ $1 != "--" ]]; then secs=$1; shift; fi
[[ $1 == "--" ]] || die "expected -- before the workload"
shift

repo=$(cd "$(dirname "$0")/.." && pwd)
out="$repo/traces/$name/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$out"

uid=$(id -u "$SUDO_USER")
rt=/run/user/$uid
wl=$(basename "$(ls "$rt"/wayland-* 2>/dev/null | grep -v '\.lock$' | head -1)")
hypr=$(ls -t "$rt/hypr" 2>/dev/null | head -1)
as_user() {
    sudo -u "$SUDO_USER" env XDG_RUNTIME_DIR="$rt" WAYLAND_DISPLAY="$wl" \
        HYPRLAND_INSTANCE_SIGNATURE="$hypr" DISPLAY="${DISPLAY:-:0}" \
        PATH="/usr/local/bin:/usr/bin:/bin" HOME="$(getent passwd "$SUDO_USER" | cut -d: -f6)" "$@"
}

card=$(ls -d /sys/class/drm/card[0-9]/device | head -1)
{
    echo "date: $(date -Is)"
    echo "kernel: $(uname -r)"
    echo "gpu_sched.sched_policy: $(cat /sys/module/gpu_sched/parameters/sched_policy)"
    for p in user_queue sched_hw_submission moverate; do
        echo "amdgpu.$p: $(cat /sys/module/amdgpu/parameters/$p 2>/dev/null || echo n/a)"
    done
    echo "dpm_level: $(cat "$card/power_dpm_force_performance_level")"
    echo "pp_dpm_sclk: $(tr '\n' ' ' < "$card/pp_dpm_sclk")"
    echo "workload: $*"
    echo "seconds: ${secs:-until exit}"
    echo "monitors:"
    as_user hyprctl monitors -j 2>/dev/null | python3 -c 'import json,sys
for m in json.load(sys.stdin):
    print("  %s %dx%d @ %.3f Hz vrr=%s" % (m["name"], m["width"], m["height"], m["refreshRate"], m["vrr"]))' || true
} > "$out/meta.txt"

events=(
    -e gpu_scheduler
    -e amdgpu:amdgpu_cs_ioctl -e amdgpu:amdgpu_sched_run_job -e amdgpu:amdgpu_bo_move
    -e amdgpu:amdgpu_vm_flush -e amdgpu:amdgpu_ib_pipe_sync
    -e drm:drm_vblank_event -e drm:drm_vblank_event_queued -e drm:drm_vblank_event_delivered
    -e dma_fence:dma_fence_emit -e dma_fence:dma_fence_signaled
)

# Background jobs of a non-interactive shell ignore SIGINT, so the samplers get SIGTERM;
# trace-cmd and bpftrace install their own SIGINT handlers and flush on it.
pids=()
samplers=()
cleanup() {
    set +e
    for p in "${samplers[@]}"; do kill -TERM "$p" 2>/dev/null; done
    for p in "${pids[@]}"; do kill -INT "$p" 2>/dev/null; done
    wait "${pids[@]}" "${samplers[@]}" 2>/dev/null
    chown -R "$SUDO_USER": "$repo/traces"
}
trap cleanup EXIT

trace-cmd record -q -C mono -b 32768 "${events[@]}" -o "$out/trace.dat" > "$out/trace-cmd.log" 2>&1 &
pids+=($!)
bpftrace "$repo/trace/bpf/ids.bt" > "$out/bpf.txt" 2> "$out/bpf.err" &
pids+=($!)
python3 "$repo/trace/clients.py" --every 0.5 > "$out/clients.jsonl" &
samplers+=($!)
python3 "$repo/trace/gpu_metrics.py" --card "${card%/device}" > "$out/gpu_metrics.csv" &
samplers+=($!)
# bpftrace needs ~3 s to load BTF and attach; start the workload only once it has
for _ in $(seq 150); do
    grep -q Attached "$out/bpf.err" 2>/dev/null && break
    sleep 0.1
done
grep -q Attached "$out/bpf.err" || echo "capture: warning: bpftrace not attached after 15 s" >&2
sleep 0.5

marker=/sys/kernel/tracing/trace_marker
echo "capture: workload_start" > "$marker"
set +e
if [[ -n $secs ]]; then
    as_user timeout -s INT "$secs" "$@" > "$out/workload.log" 2>&1
else
    as_user "$@" > "$out/workload.log" 2>&1
fi
rc=$?
set -e
echo "capture: workload_end rc=$rc" > "$marker"
sleep 0.5
echo "capture: wrote $out (workload exit $rc)"
