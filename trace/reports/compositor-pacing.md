# Compositor pacing: the extra refresh is conditional

Trace/source study, 2026-11-04; completes the existing-capture investigation
formerly at plan.md Next 1. No compositor configuration or kernel was changed.

**Finding:** m2's extra refresh under light vkmark load is real, but it is not
explained by a longer wait to the next Hyprland GPU submission. It also does not
occur in every later loaded run. The captures do not identify surface receipt,
buffer latch or which client buffer a compositor job sampled, so the exact
WSI/compositor mechanism remains unresolved.

## Measurement and reproduction

`trace/pacing.py` reads `burst.csv` and `trace-cmd report` without modifying a
capture. It compares client timestamps with Hyprland gfx **queue** events and
`drm_vblank_event`'s embedded hardware timestamp (`time=`, not the trace event's
CPU timestamp). Output selection requires agreement with presentation feedback's
MSC **and** timestamp within 1 ms. Missing or ambiguous output matches fail.
The `matched` column counts feedback/vblank matches for the entire run; an
occasional untraced vblank does not discard an otherwise presented frame.

For example, from the repository root:

```sh
python3 trace/pacing.py traces/m2/auto-none-r?/*/ traces/m2/auto-vkmark-cube-r?/*/ --csv /tmp/pacing-m2.csv
python3 trace/pacing.py traces/m3-p{0,2}/auto-{none,vkmark-cube}-r?/*/ traces/m3-p1r/auto-{none,vkmark-cube}-r?/*/
python3 trace/pacing.py traces/m3-p1/auto-none-r?/*/ traces/m3-p1/auto-vkmark-cube-r[13]/*/
python3 -m unittest discover -s trace -p 'test_pacing.py'
```

[Per-run results](compositor-pacing-runs.txt) cover m2 auto none/vkmark/vk-hog/
vk-hog1 (three repetitions each), plus none/vkmark in m3-p1, m3-p0, m3-p2 and
m3-p1r (three repetitions each except m3-p1 vkmark: its failed r2 has an
empty burst log and is excluded). Frames 0 and 1+ are separate. This is a
descriptive study of existing sessions, not an independent-session treatment
estimate. The script excludes discarded/pending feedback and frames without
bracketing Hyprland submissions or a preceding recorded vblank; `n` is the
remaining count. In particular m3-p0 vkmark r2 has only 145 presented frames 1+.

`t_intended` on paced frames is the **arrival** of the prior commit's frame
callback at the client. `t_present` is just before `vkQueuePresentKHR`, with
`t_present_ret` just after it. Neither establishes when Mesa actually commits,
when Hyprland processes that commit, or when its buffer becomes current.
The metrics deliberately use CPU and presentation clocks; loose GPU timestamp
calibration does not affect them. Hyprland gfx submissions are not tagged with
surface or output, and their occurrence is not proof of victim composition.

## Results

For m2 auto, frames 1+, the entries below are **ranges of the three per-run
medians**, in ms (180 presented frames per run):

| Background | Callback → submit | Present call → next Hyprland queue | Next Hyprland queue → presentation | Callback → presentation |
|---|---:|---:|---:|---:|
| none | 0.043–0.050 | 16.472–16.500 | 16.895–16.944 | 33.489–33.532 |
| vkmark-cube | 0.057–0.067 | 16.443–16.450 | 33.686–33.730 | 50.215–50.266 |
| vk-hog | 0.044–0.067 | 16.474–16.522 | 33.666–33.774 | 50.272–66.720 |
| vk-hog1 | 0.047–0.052 | 16.455–16.472 | 33.672–33.769 | 50.113–50.309 |

The heavy-hog tail can add another refresh. Medians of components need not sum
to the median total. vkmark is the useful light-GPU-interference control.

The prior Hyprland queue precedes the present call by only 0.18–0.25 ms under
these loads (0.21–0.22 ms alone). There are **no Hyprland queue events between
callback arrival and present call in the loaded m2 frames 1+**. Alone, this
happens for only 1/180 frames per run. Thus the original hypothesis, that a new
commit misses a compositor render opportunity, is plausible in a broad sense,
but it does not by itself explain why only the loaded m2 path gains a refresh:
the next compositor submission is equally far away in both conditions. The
additional delay appears in the interval *after* that submission, without
proving that it used this frame. Buffer queuing/availability and compositor
latching remain candidates, not established causes.

Frame callbacks arrive ~16.3–16.6 ms after the preceding recorded hardware
vblank in m2 steady state, near the next 16.667 ms boundary. Do not describe
that as “right after vblank.” The script reports time since the **last recorded**
vblank, not a phase extrapolated through missing events. After idle it can
exceed a refresh by hundreds of milliseconds because unused vblank events are
absent. Frame 0 is unpaced and its `callback_*` columns refer to burst intent,
not a callback.

Later captures disprove a universal 50 ms steady-state rule. For vkmark, the
per-run callback → presentation medians are:

| Matrix | Policy | r1 / r2 / r3 (ms) | Active outputs in metadata |
|---|---|---|---|
| m3-p1 | FIFO | 33.856 / failed / 33.840 | eDP-1 |
| m3-p0 | RR | 33.965 / 50.474 / 33.943 | eDP-1 |
| m3-p2 | fair | 33.944 / 33.930 / 33.949 | eDP-1 |
| m3-p1r | FIFO repeat | 50.260 / 50.413 / 50.397 | eDP-1 + DP-1 |

m3-p1, m3-p0 and m3-p2 alone have ~32.1–32.3 ms medians. Under vkmark their wait to the
next Hyprland submission is still ~16.6 ms; the post-submission interval is
~17.3 ms in the 34 ms runs and ~33.9 ms in the 50 ms run. These observations
locate the conditional extra refresh without assigning it to scheduler policy.

**New confound:** all 16 m3-p1r `meta.txt` files list both eDP-1 at 60 Hz and
DP-1 at 75 Hz. All m2 and initial m3-p0/p1/p2 metadata files list only eDP-1.
Feedback selects CRTC 0 at 60 Hz in the inspected p1r runs, but Hyprland's jobs
can include either output. The earlier claim that the p1r repeat isolated
boot/session age on a single display was incorrect. It cannot distinguish boot
age, output count and policy. A fresh FIFO repeat must verify DP-1 is disabled
or disconnected, rather than assuming it remained unplugged.

## Hyprland source and options

Inspected upstream `v0.56.0`, commit
`36b2e0cfe0c6094dbc47bd42a437431315bb3087`. This is the nominal recorded version,
not proof of the precise packaged binary or its capture-time configuration.

- [MonitorFrameScheduler.cpp](https://github.com/hyprwm/Hyprland/blob/36b2e0cfe0c6094dbc47bd42a437431315bb3087/src/output/MonitorFrameScheduler.cpp):
  the normal path renders on output frame events; the optional path tracks render
  completion and can prepare a third frame after a missed refresh.
- [Monitor.cpp](https://github.com/hyprwm/Hyprland/blob/36b2e0cfe0c6094dbc47bd42a437431315bb3087/src/output/Monitor.cpp):
  output presentation may send client callbacks when no monitor render is needed.
- [Renderer.cpp](https://github.com/hyprwm/Hyprland/blob/36b2e0cfe0c6094dbc47bd42a437431315bb3087/src/render/Renderer.cpp):
  the no-damage path also sends callbacks. Callback arrival is not a latch marker.
- [ConfigValues.cpp](https://github.com/hyprwm/Hyprland/blob/36b2e0cfe0c6094dbc47bd42a437431315bb3087/src/config/values/ConfigValues.cpp):
  defaults are `render:new_render_scheduling=false`, `debug:vfr=true`,
  `debug:damage_tracking=2`, `render:direct_scanout=0`. These are defaults,
  not captured settings.

The old render-ahead options were removed in
[Hyprland 0.50](https://hypr.land/news/update50/). They are not a fix to try on
0.56. Its [versioned options reference](https://wiki.hypr.land/0.56.0/Configuring/Basics/Variables/)
contains the optional new scheduler. Changing it would be a separate compositor
experiment; no capture here establishes whether it was enabled. VFR controls
idle rendering, not a target latency, and disabling it changes the baseline.

## Consequences and next measurement

Keep measured GPU delay separate from presentation delay. Phase 2 should model
callback-driven clients, conditional buffering/presentation latency and output
configuration; do not fit a fixed “one extra refresh whenever loaded” rule.
The existing limit-on-in-flight-work result remains a GPU-start result. Its
presentation benefit must be measured, rather than inferred from that result.

To resolve the remaining mechanism, capture matched idle/vkmark conditions with
fixed geometry and one verified active output, recording exact Hyprland/Mesa
builds and effective render options. Correlate Wayland client commit dispatch,
server receipt, buffer readiness (including acquire fences), compositor latch,
output commit and presentation with a surface/frame identity. A short
`WAYLAND_DEBUG=client` log can locate actual client requests, but cannot alone
establish server latch; it also needs timestamp alignment and an overhead check.
Add compositor instrumentation for the latter before interpreting the next
GPU job as the victim's render. Use independent repeated sessions and retain
FIFO present mode as the primary baseline; any present-mode or render-option
change is a separate treatment. This follow-up needs new captures; the
existing scheduler/vblank traces cannot recover those missing events.
