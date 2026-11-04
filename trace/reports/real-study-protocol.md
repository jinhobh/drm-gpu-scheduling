# Real-application attribution study: tooling and capture protocol

2026-11-04. This is a prospective protocol, not new hardware evidence. The root
captures and presentation instrumentation remain pending. Keep the frozen simulator
and policy unchanged until the measured workload models are available.

## Workloads

`trace/workloads/real.py SCENARIO` runs on two newly allocated Hyprland workspaces,
restores the original workspace, and terminates only its owned process groups on
exit or interruption. It requires eDP-1 as the only active output. It never disables
a display or changes DPM/kernel settings. Leave the machine untouched during a run.

| Scenario | Action | Interpretation |
|---|---|---|
| `gtk-launch` | Launch GTK4/libadwaita Zenity text viewer, observe mapping, hold, close; repeat | A real lightweight GTK process-launch case, not a representative app suite. Polling observes mapping within ~20 ms plus IPC/scheduling time; it does not measure first presentation. Warm page-cache launches after the first trial are a separate population. |
| `firefox-scroll` | Separate Firefox profile, local deterministic page, alternating Page Down/Up using wtype | Normal Firefox smooth scrolling with controlled local content; scripted key request/return bracket injection, not input receipt. Browser startup is warmup, not a launch measurement. Abort before injecting if fixture loses focus. |
| `compositor` | Map a GTK window; switch to empty workspace and back | Hyprland HIGH-priority workspace animations. Separate map actions from workspace actions. Only `none`/off-screen `gl-hog` allowed so hiding the workspace does not hide a windowed hog. |
| `mpv` | Continuous local-file playback, Vulkan gpu-next, software decode, no audio | Video comparison victim. `video-stats.lua` samples drop/mistiming/delay counters every 100 ms. These are mpv diagnostics, not surface presentation timestamps. |

All scenarios accept `--hog NAME` from `hog.sh --names` and `--cpu-workers N`.
CPU workers do integer/hash work without GPU use. They are an added CPU-pressure
control, not a matched simulation of an application's CPU path. Start with one,
then repeat at the available logical CPU count; record actual CPU pressure before
claiming CPU saturation. `--hog mpv --load-clip /absolute/clip` adds video as a
background load. `hog.sh mpv` also works with `burst.sh`/`matrix.sh` when `MPV_CLIP`
is set. The clip is local, looped, and software decoded in all arms; use hardware
decode only as a separately named future control.

`events.jsonl` uses host CLOCK_MONOTONIC nanoseconds for action requests/returns,
process IDs, map observations, per-trial client/monitor snapshots and completion.
Do not compare mpv's `mpv_time` directly with these nanoseconds: its time-base offset
is not calibrated here. App logs, isolated Firefox profile, and clip SHA256 are
saved in the capture directory. Window geometry is observed, not enforced by this
first tool version; reject inconsistent geometry before comparing runs. A tiled
hog can change GTK/browser geometry. Pilot and fix the configuration before the
measurement matrix; never pool unmatched layouts as in m2.

## Commands

Disable/unplug DP-1 yourself and verify only eDP-1 is active. Do not change the
stock kernel or boot default. `--check` opens no windows:

```sh
python3 /home/jinho/Projects/drm-gpu-scheduling/trace/workloads/real.py gtk-launch --check
make -C /home/jinho/Projects/drm-gpu-scheduling/trace/burst_client
```

Prepare a reproducible local test clip without a network dependency (this test
pattern is a video control, not natural-content validation):

```sh
mkdir -p /home/jinho/Projects/drm-gpu-scheduling/traces/assets
ffmpeg -f lavfi -i testsrc2=size=1280x720:rate=60 -t 20 -c:v libx264 -crf 18 -pix_fmt yuv420p /home/jinho/Projects/drm-gpu-scheduling/traces/assets/testsrc-720p60.mp4
```

First run short no-root pilots, one scenario at a time with a fresh `--out` directory:

```sh
python3 /home/jinho/Projects/drm-gpu-scheduling/trace/workloads/real.py gtk-launch --trials 2 --out /home/jinho/Projects/drm-gpu-scheduling/traces/real-pilot-gtk
python3 /home/jinho/Projects/drm-gpu-scheduling/trace/workloads/real.py firefox-scroll --trials 2 --out /home/jinho/Projects/drm-gpu-scheduling/traces/real-pilot-firefox
python3 /home/jinho/Projects/drm-gpu-scheduling/trace/workloads/real.py compositor --trials 2 --out /home/jinho/Projects/drm-gpu-scheduling/traces/real-pilot-compositor
python3 /home/jinho/Projects/drm-gpu-scheduling/trace/workloads/real.py mpv --trials 2 --clip /home/jinho/Projects/drm-gpu-scheduling/traces/assets/testsrc-720p60.mp4 --out /home/jinho/Projects/drm-gpu-scheduling/traces/real-pilot-mpv
```

After pilot geometry/focus validation, the user runs root captures (change the
capture name for each independent run; repeat each scenario):

```sh
sudo /home/jinho/Projects/drm-gpu-scheduling/trace/capture.sh real-gtk-none -- /home/jinho/Projects/drm-gpu-scheduling/trace/workloads/real.py gtk-launch --hog none
sudo /home/jinho/Projects/drm-gpu-scheduling/trace/capture.sh real-gtk-vk -- /home/jinho/Projects/drm-gpu-scheduling/trace/workloads/real.py gtk-launch --hog vk-hog
sudo /home/jinho/Projects/drm-gpu-scheduling/trace/capture.sh real-scroll-cpu -- /home/jinho/Projects/drm-gpu-scheduling/trace/workloads/real.py firefox-scroll --cpu-workers 1
sudo /home/jinho/Projects/drm-gpu-scheduling/trace/capture.sh real-video-vk -- /home/jinho/Projects/drm-gpu-scheduling/trace/workloads/real.py mpv --hog vk-hog --clip /home/jinho/Projects/drm-gpu-scheduling/traces/assets/testsrc-720p60.mp4
```

`capture.sh` now saves Hyprland version/build, package versions, render-option
queries (including unsupported-option errors), raw monitor JSON, boot ID/uptime,
kernel command line and repository revision/dirty summary. Preserve these with
all captures. This is supporting provenance, not Arch module/source attestation.
For each completed run use `python3 trace/parse.py /absolute/run` to produce
`jobs.csv`; use `events.jsonl`, `clients.jsonl`, BPF IDs and snapshots to identify
the victim's client IDs (Firefox children may own GPU submissions). Do not select
only the launcher PID. `join.py` expects burst_client CSV and is not a real-app
presentation joiner. Real-app per-action attribution/plotting is still pending.

## Measurement and statistical plan

Run each victim alone, with heavy Vulkan (`vk-hog`, `vk-hog1`), heavy GL (`gl-hog`),
tiny-job flood (`vkmark-cube`), and video load where applicable. Add CPU-only and
CPU+GPU control cells to separate action→submission changes from queue/ring waits.
Compositor animation cells use off-screen GL only until equivalent always-visible
Vulkan load is available. Keep DPM auto and GFXOFF on for the primary study; use
the existing pinned-power calibration as a separately labeled control.

Before any presentation claim, implement Next 3's actual Wayland commit/receipt/
latch and buffer-readiness instrumentation, or an independently validated visual
measurement method. Input action→map and job completion→next vblank are insufficient.
Record actual presented/discarded buffers, output refresh grid and missing feedback.
Primary outcomes: action→first presentation and missed refreshes during a burst.
Secondary: action→submission, dependency/order/pick/credit/ring/exec breakdown,
hog throughput and mpv diagnostic counter deltas. Report stalls without completed
jobs and failed/missing feedback rather than dropping those trials.

Use at least five independent boot/login-session blocks as an initial precision
pilot, not a promise of statistical power. Within each block randomize condition
order, save the order before running, and hold output, geometry, warmup, DPM,
application/profile settings, and background services constant. In future kernel
A/B work, counterbalance policy boot order across blocks on the same kernel base.
Mark first-launch vs warm-launch trials; do not clear system caches on the daily
laptop. Profile creation is repeated per run, so profile state must be treated as
a controlled part of the workload. Save exact clip and HTML hashes/build versions.

Summarize first-frame and steady-burst outcomes per run. Pair conditions within
session blocks; report paired differences and 95% confidence intervals from a
block bootstrap resampling independent sessions (retain complete runs/trials
within sampled blocks). Do not bootstrap frames as independent observations.
With five blocks uncertainty estimates are preliminary: add blocks until the
interval resolves the prespecified practically relevant change (one refresh for
presentation, and the measured hog-job cost for start latency) or a predefined
resource cap is reached. Declare the cap before the confirmatory matrix. Avoid
p99 inference from a few dozen first frames; show distributions/maxima with counts
and uncertainty limits. Select held-out workloads before tuning phase 2 models.

The 1.6 report will combine validated real-app captures and the existing m1–m3
evidence, show latency-breakdown/presentation plots, independent-session uncertainty,
layout/output/session confounds, and the pinned prior-art comparison. No new report
conclusion or workshop submission decision is supported by tooling alone.
