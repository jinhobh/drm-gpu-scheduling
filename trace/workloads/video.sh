#!/usr/bin/env bash
# Local clip only. CPU decoding makes the decode mode explicit across runs.
set -euo pipefail
clip=${1:-${MPV_CLIP:-}}
[[ -f $clip ]] || { echo 'video.sh: provide a local clip (argument or MPV_CLIP)' >&2; exit 2; }
here=$(cd "$(dirname "$0")" && pwd)
exec mpv --no-config --load-scripts=no --no-audio --loop-file=inf --vo=gpu-next --gpu-api=vulkan \
    --gpu-context=waylandvk --hwdec=no --video-sync=display-resample \
    --geometry=960x540 --script="$here/video-stats.lua" -- "$clip"
