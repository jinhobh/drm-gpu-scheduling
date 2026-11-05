#!/usr/bin/env bash
# Show or set the GPU DPM performance level (root to set).
#
#   trace/dpm.sh                 # status
#   sudo trace/dpm.sh high       # pin sclk to the highest level (removes clock ramp-up)
#   sudo trace/dpm.sh auto       # back to the default
set -euo pipefail

dev=$(ls -d /sys/class/drm/card[0-9]/device | head -1)
if [[ $# -gt 0 ]]; then
    case $1 in
        auto|low|high|manual|profile_standard|profile_min_sclk|profile_peak) ;;
        *) echo "dpm: unknown level $1" >&2; exit 1 ;;
    esac
    echo "$1" > "$dev/power_dpm_force_performance_level"
fi
echo "level: $(cat "$dev/power_dpm_force_performance_level")"
echo "sclk:  $(tr '\n' ' ' < "$dev/pp_dpm_sclk")"
