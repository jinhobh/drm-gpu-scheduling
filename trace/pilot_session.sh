#!/usr/bin/env bash
# Run one whole pilot session from a pilot.py plan, after checking the boot is right.
#
#   sudo /home/jinho/Projects/drm-gpu-scheduling/trace/pilot_session.sh PLAN.csv BLOCK SESSION
#   trace/pilot_session.sh --check PLAN.csv BLOCK SESSION   # checks + command list, no root
#
# `python3 trace/pilot.py status PLAN.csv` says which session is next and how to boot it.
# Refuses to start unless: the drmsched test kernel runs with exactly this arm's command-line
# selectors (and sysfs agrees), eDP-1 is the only active output, no GPU reset happened in
# this boot (the GPU timestamp counter resets), and the session has not been started before.
# Runs the session's commands in order as root (the `sudo` prefix is dropped: SUDO_USER is
# kept, so capture.sh still runs workloads as the user), logs everything to
# traces/pilot-sessions/MATRIX.log, continues past a failed capture but stops at a GPU reset,
# and leaves MATRIX.done (all succeeded) or MATRIX.incomplete (lists what failed).
set -uo pipefail

check_only=0
if [[ ${1:-} == --check ]]; then check_only=1; shift; fi
usage="usage: sudo trace/pilot_session.sh [--check] PLAN.csv BLOCK SESSION"
plan=${1:?$usage}; block=${2:?$usage}; session=${3:?$usage}
repo=$(cd "$(dirname "$0")/.." && pwd)
die() { echo "pilot_session: $*" >&2; exit 1; }

if (( ! check_only )); then
    [[ $EUID -eq 0 && -n ${SUDO_USER:-} ]] || die "run with sudo from your session"
fi
user=${SUDO_USER:-$USER}
rt=/run/user/$(id -u "$user")
hypr=$(ls -t "$rt/hypr" 2>/dev/null | head -1)

cmds=$(mktemp)
trap 'rm -f "$cmds"' EXIT
mapfile -t info < <(python3 - "$plan" "$block" "$session" "$cmds" <<'EOF'
import csv, sys
plan, block, session, out = sys.argv[1:]
rows = [r for r in csv.DictReader(open(plan, newline=""))
        if r["block"] == block and r["session"] == session]
if len(rows) != 1:
    sys.exit(f"pilot_session: no unique block {block} session {session} in {plan}")
r = rows[0]
open(out, "w").write(r["commands"].rstrip("\n") + "\n")
print(r["arm"]); print(r["cmdline"]); print(r["matrix"])
EOF
) || exit 1
[[ ${#info[@]} -eq 3 ]] || die "could not read the plan"
arm=${info[0]} cmdline=${info[1]} matrix=${info[2]}
echo "pilot_session: block $block session $session arm $arm ($matrix)"

# --- checks -------------------------------------------------------------------------------
fail=0
bad() { echo "  FAIL: $*" >&2; fail=1; }
[[ $(uname -r) == 7.2.11-arch1-1-drmsched ]] || bad "kernel is $(uname -r), not the drmsched test kernel"
read -r -a running < /proc/cmdline
for tok in $cmdline; do
    printf '%s\n' "${running[@]}" | grep -qxF -- "$tok" || bad "boot command line lacks $tok"
    if [[ $tok =~ ^(gpu_sched|amdgpu)\.([a-z_]+)=(.*)$ ]]; then
        f=/sys/module/${BASH_REMATCH[1]}/parameters/${BASH_REMATCH[2]}
        v=$(cat "$f" 2>/dev/null)
        [[ $v == "${BASH_REMATCH[3]}" ]] || bad "$f is '$v', expected ${BASH_REMATCH[3]}"
    fi
done
outputs=$(sudo -u "$user" env XDG_RUNTIME_DIR="$rt" HYPRLAND_INSTANCE_SIGNATURE="$hypr" \
    hyprctl monitors -j 2>/dev/null | python3 -c 'import json,sys
print(" ".join(m["name"] for m in json.load(sys.stdin) if not m.get("disabled")))' 2>/dev/null)
[[ $outputs == eDP-1 ]] || bad "active outputs are '${outputs:-unknown}', need eDP-1 only (unplug or disable DP-1)"
resets() { journalctl -k -b -o cat 2>/dev/null | grep -c "GPU reset begin"; }
r0=$(resets)
[[ $r0 == 0 ]] || bad "a GPU reset happened in this boot; reboot into the arm again first"
for d in "$repo/traces/$matrix-pre" "$repo/traces/$matrix" "$repo/traces/pilot-sessions/$matrix.log"; do
    [[ -e $d ]] && bad "session already started ($d exists); do not reuse it, ask before rerunning"
done
(( fail )) && die "checks failed; nothing was run"
echo "pilot_session: checks passed (kernel, arm $arm, eDP-1 only, no GPU reset, new session)"

n=$(grep -c . "$cmds")
if (( check_only )); then
    echo "pilot_session: $n commands (run with sudo to execute):"
    sed 's/^/  /' "$cmds"
    exit 0
fi

# --- run ----------------------------------------------------------------------------------
logdir=$repo/traces/pilot-sessions
mkdir -p "$logdir"
chown "$user": "$repo/traces" "$logdir" 2>/dev/null
log=$logdir/$matrix.log
{
    echo "# pilot session block=$block session=$session arm=$arm matrix=$matrix"
    echo "# plan=$plan sha256=$(sha256sum "$plan" | cut -d' ' -f1)"
    echo "# boot_id=$(cat /proc/sys/kernel/random/boot_id) uptime_s=$(cut -d' ' -f1 /proc/uptime)"
    echo "# cmdline=$(cat /proc/cmdline)"
} > "$log"
echo "pilot_session: $n commands, about 20-25 minutes. Do not use the laptop until it finishes."
failed=()
i=0
while IFS= read -r line; do
    [[ -n $line ]] || continue
    i=$((i + 1))
    line=${line#sudo }
    echo "=== [$i/$n] $(date -Is) $line" | tee -a "$log"
    bash -c "$line" < /dev/null 2>&1 | tee -a "$log"  # stdin: never the command list
    rc=${PIPESTATUS[0]}
    echo "=== [$i/$n] exit $rc" | tee -a "$log"
    (( rc == 0 )) || failed+=("[$i] exit $rc: $line")
    if [[ $(resets) != "$r0" ]]; then
        echo "pilot_session: GPU reset during the session: stopping (safety failure)" | tee -a "$log"
        printf '%s\n' "GPU reset after command $i" "${failed[@]}" > "$logdir/$matrix.incomplete"
        chown "$user": "$log" "$logdir/$matrix.incomplete"
        exit 2
    fi
done < "$cmds"

if (( ${#failed[@]} )); then
    printf '%s\n' "${failed[@]}" > "$logdir/$matrix.incomplete"
    echo "pilot_session: finished with ${#failed[@]} failed command(s); see $log" | tee -a "$log"
    chown "$user": "$log" "$logdir/$matrix.incomplete"
    exit 1
fi
date -Is > "$logdir/$matrix.done"
echo "pilot_session: session complete. Reboot normally (or into the next session's arm)." | tee -a "$log"
chown "$user": "$log" "$logdir/$matrix.done"
