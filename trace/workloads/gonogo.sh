#!/usr/bin/env bash
# Step 0.2 workload: a Vulkan hog, a video, compositor animations and an app launch, so
# every kind of client shows up in one short trace. Runs as the user (capture.sh does that).
# It rearranges windows on the current workspace for ~12 s and closes what it opened.
set -u

pids=()
cleanup() { for p in "${pids[@]}"; do kill "$p" 2>/dev/null; done; wait 2>/dev/null; }
trap cleanup EXIT

echo "t=0 start vkmark (uncapped Vulkan hog)"
vkmark --run-forever -b cube -p mailbox &
pids+=($!)
echo "t=0 start mpv (60 fps test video)"
mpv --no-config --really-quiet --loop=inf --vo=gpu-next \
    "av://lavfi:testsrc2=size=1280x720:rate=60" &
pids+=($!)
sleep 3

echo "t=3 compositor animations (swapnext x10)"
for _ in $(seq 10); do hyprctl dispatch swapnext > /dev/null; sleep 0.3; done

echo "t=6 app launch (kitty)"
kitty --class gonogo-kitty sh -c 'sleep 3' &
pids+=($!)
sleep 4

echo "t=10 compositor animations (swapnext x6)"
for _ in $(seq 6); do hyprctl dispatch swapnext > /dev/null; sleep 0.3; done
echo "done"
