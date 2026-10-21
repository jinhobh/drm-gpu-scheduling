#!/usr/bin/env bash
# First attribution matrix (plan.md, Next): traced burst_client runs, one hog each, under
# DPM auto and high, repeated, in random order; plus a --calib run per DPM level.
#
#   sudo /home/jinho/Projects/drm-gpu-scheduling/trace/matrix.sh [NAME]
#
# Env: REPS (3), HOGS ("none vk-hog glmark2-terrain vkmark-cube"), DPMS ("auto high"),
#      GFXOFF_OFF=1 adds a third condition: DPM high with GFXOFF disabled (debugfs),
#      BURST_ARGS ("--iters 736 -n 10 -i 300 -b 20"), DRY=1 prints the order only.
# Output: traces/NAME/<cond>-<hog>-r<k>/<timestamp>/ (NAME defaults to m-<date-time>), and
# traces/NAME/order.txt, log in traces/NAME/matrix.log. DPM goes back to auto and GFXOFF back
# on at exit, also on Ctrl-C. A failed run is logged and skipped.
# Takes ~10 min with the defaults, ~15 with GFXOFF_OFF=1. Leave the laptop alone while it runs (input, other
# windows and video change the GPU load).
set -euo pipefail

die() { echo "matrix: $*" >&2; exit 1; }
[[ ${DRY:-0} == 1 || ( $EUID -eq 0 && -n "${SUDO_USER:-}" ) ]] || die "run with sudo from your session"

repo=$(cd "$(dirname "$0")/.." && pwd)
name=${1:-m-$(date +%Y%m%d-%H%M)}
reps=${REPS:-3}
hogs=(${HOGS:-none vk-hog glmark2-terrain vkmark-cube})
conds=(${DPMS:-auto high})
[[ ${GFXOFF_OFF:-0} == 1 ]] && conds+=(high-nogfxoff)
burst_args=(${BURST_ARGS:---iters 736 -n 10 -i 300 -b 20})
known=" $("$repo/trace/workloads/hog.sh" --names) "
for h in "${hogs[@]}"; do
    [[ $known == *" $h "* ]] || die "unknown hog '$h' (known:$known)"
done

# amdgpu_gfxoff takes binary u32 writes (length a multiple of 4; `echo 0` is EINVAL): 0 adds
# a GFXOFF-disable request, 1 drops one (and WARNs if there is none), so track our own.
gfxoff=""
gfxoff_held=0
if [[ ${DRY:-0} != 1 ]]; then
    gfxoff=$(find -L /sys/kernel/debug/dri -maxdepth 2 -name amdgpu_gfxoff 2>/dev/null | head -1)
    [[ " ${conds[*]} " != *" high-nogfxoff "* || -n $gfxoff ]] || die "no amdgpu_gfxoff in debugfs"
fi
gfxoff_off() {
    ((gfxoff_held)) && return 0
    printf '\x00\x00\x00\x00' > "$gfxoff" || return 1
    gfxoff_held=1
    echo "matrix: GFXOFF disabled, status $(od -An -tu4 -N4 "${gfxoff}_status" 2>/dev/null)"
}
gfxoff_on() {
    ((gfxoff_held)) || return 0
    printf '\x01\x00\x00\x00' > "$gfxoff" || return 1
    gfxoff_held=0
}

restore() {
    set +e
    [[ ${DRY:-0} == 1 ]] && return
    gfxoff_on
    "$repo/trace/dpm.sh" auto
    chown -R "$SUDO_USER": "$repo/traces/$name" 2>/dev/null
}
trap restore EXIT

# fails (and the run is skipped) if a DPM or GFXOFF write fails, so no run is mislabelled
set_cond() {
    case $1 in
    auto) "$repo/trace/dpm.sh" auto > /dev/null && gfxoff_on || return 1 ;;
    high) "$repo/trace/dpm.sh" high > /dev/null && gfxoff_on || return 1 ;;
    high-nogfxoff) "$repo/trace/dpm.sh" high > /dev/null && gfxoff_off || return 1 ;;
    esac
    sleep 2  # let clocks settle
}

# every (cond, hog, rep) once, plus one calib run per condition, shuffled
runs=()
for c in "${conds[@]}"; do
    runs+=("$c calib 1")
    for h in "${hogs[@]}"; do
        for r in $(seq "$reps"); do runs+=("$c $h $r"); done
    done
done
mapfile -t runs < <(printf '%s\n' "${runs[@]}" | shuf)

mkdir -p "$repo/traces/$name"
[[ ${DRY:-0} == 1 ]] || exec > >(tee -a "$repo/traces/$name/matrix.log") 2>&1
printf '%s\n' "${runs[@]}" > "$repo/traces/$name/order.txt"
echo "matrix: ${#runs[@]} runs -> traces/$name (order in order.txt)"
[[ ${DRY:-0} == 1 ]] && { cat "$repo/traces/$name/order.txt"; rm -r "$repo/traces/$name"; exit 0; }

i=0
for run in "${runs[@]}"; do
    read -r c h r <<< "$run"
    i=$((i + 1))
    echo "matrix: [$i/${#runs[@]}] $c $h r$r"
    set_cond "$c" || { echo "matrix: setting $c failed, run skipped"; continue; }
    if [[ $h == calib ]]; then
        args=(none --calib --iters 736); dir=$c-calib
    else
        args=("$h" "${burst_args[@]}"); dir=$c-$h-r$r
    fi
    "$repo/trace/capture.sh" "$name/$dir" -- "$repo/trace/workloads/burst.sh" "${args[@]}" \
        || echo "matrix: run $dir failed (exit $?)"
done
echo "matrix: done. Analyse with: python3 trace/join.py traces/$name/*/*/"
