#!/usr/bin/env bash
# Run one background GPU load in the foreground until killed (SIGINT/SIGTERM).
#
#   trace/workloads/hog.sh NAME
#
# Per-frame cost at 1280x1400 (half of DP-1, Hyprland tiling), measured 2026-10-18,
# DPM auto, FIFO sched_policy (from m3 on, glmark2/vkmark/stk tile alone on an empty eDP-1
# workspace, ~1904x1163, so their cost differs; vk-hog's is set by -c on a fixed 512x512):
#   glmark2-terrain   GL,     ~7.2 ms/frame (140 fps uncapped)
#   glmark2-refract   GL,     ~7.0 ms/frame (143 fps)
#   gl-hog            GL, glmark2 terrain rendered off-screen at 3840x2160: ~8.5 ms/frame
#                     (119 fps, eDP-1 only, 2026-10-18), independent of the window layout
#   stk               GL,     114 fps but CPU-bound: gfx only ~30% busy (~2.6 ms GPU/frame);
#                     a game *comparison* load, not a heavy hog (AI race, lighthouse)
#   vk-hog            Vulkan, 8.0 ms/frame exactly (burst_client, mailbox, 114 fps)
#   vk-hog1           vk-hog with 1 frame in flight instead of 2: the ring holds at most one
#                     of its jobs, what the policy's one-NORMAL-job-on-the-ring rule would do
#   vkmark-cube       Vulkan, job flood: ~0.03-0.05 ms jobs at ~29 000 fps
#   none              sleep
#
#   trace/workloads/hog.sh --names    prints the valid names
set -u
names="gl-hog glmark2-terrain glmark2-refract stk vk-hog vk-hog1 vkmark-cube none"
here=$(cd "$(dirname "$0")" && pwd)
size=${HOG_SIZE:-1280x1000}  # vk-hog floats at this size: keep it inside the 1920x1200 panel
case ${1:-} in
glmark2-terrain) exec glmark2-wayland --size "$size" --run-forever -b terrain ;;
gl-hog) exec glmark2-wayland --off-screen --size 3840x2160 --run-forever -b terrain ;;
glmark2-refract) exec glmark2-wayland --size "$size" --run-forever -b refract ;;
stk) exec supertuxkart -R --track=lighthouse --laps=99 --profile-time=3600 \
         --screensize="$size" --windowed ;;
vk-hog) exec "$here/../burst_client/burst_client" --present mailbox --pace fifo -i 0 -n 1000 \
            -c "${HOG_COST_US:-8000}" --size "$size" -o "${HOG_LOG:-/dev/null}" ;;
vk-hog1) exec "$here/../burst_client/burst_client" --present mailbox --pace fifo -i 0 -n 1000 \
            --inflight 1 -c "${HOG_COST_US:-8000}" --size "$size" -o "${HOG_LOG:-/dev/null}" ;;
vkmark-cube) exec vkmark --run-forever -b cube -p mailbox --size "$size" ;;
none) exec sleep infinity ;;
--names) echo "$names" ;;
*) echo "usage: hog.sh ${names// /|}" >&2; exit 2 ;;
esac
