# Draft: drm/sched fair repeated re-entry delays a waiting burst behind tiny jobs

Prepared 2026-11-04. Review draft only; no mail or issue has been sent.

Suggested subject: `[BUG] drm/sched fair: tiny-job re-entry repeatedly overtakes a waiting burst`

I observed a waiting NORMAL-priority burst delayed by hundreds of subsequent
NORMAL vkmark jobs under `gpu_sched.sched_policy=2`. In three captures, 21/60
first frames after idle took at least 5 ms from Vulkan submit to first GPU
timestamp, with a maximum of 15.528 ms. The tiny jobs executing ahead were
approximately 0.03–0.05 ms each, so this differs from blocking behind a single
long non-preemptible job. Expected behavior is that sustained GPU use eventually
counts against the flood even when each pop empties its software queue.

This appears related to the repeated-re-entry unfairness discussed in the
[August min_vruntime proposal](https://www.mail-archive.com/dri-devel@lists.freedesktop.org/msg628299.html),
which [was withdrawn](https://www.mail-archive.com/dri-devel@lists.freedesktop.org/msg628326.html).
I am reporting additional workload evidence, not a newly discovered bug class
or a tested fix. Is an accounting/placement repair under development, and would
instrumenting runtime/prev_runtime and restore placement help validate it?

## Affected setup and source check

Hardware captures were made on 2026-10-19 with:

- AMD Ryzen AI 7 350 / Radeon 860M, RADV KRACKAN1 (GC 11.5.2).
- Arch `7.2.11-arch1-1`, `gpu_sched.sched_policy=2`,
  `amdgpu.sched_hw_submission=2`, `amdgpu.user_queue=-1`; the attribution study
  found clients using drm_sched, not user queues.
- Hyprland 0.56.0, Mesa 26.2.5, vkmark 2025.01. These userspace versions come
  from the study record and workload logs; exact build hashes were not captured.
- Only eDP-1 active, 1920×1200 at 60.001 Hz, VRR off; DPM auto, GFXOFF enabled.
- vkmark mailbox cube flood and a fixed-size floating Vulkan/Wayland burst
  client; both NORMAL, with HIGH Hyprland work sharing gfx.

On 2026-11-04 I checked the official drm-misc-next branch using both
`git ls-remote` and its branch API. Both resolved to
`e32e83a6c5ec031a7f59d4794113d8484b220254`.
Its [sched_rq.c](https://gitlab.freedesktop.org/drm/misc/kernel/-/blob/e32e83a6c5ec031a7f59d4794113d8484b220254/drivers/gpu/drm/scheduler/sched_rq.c)
is byte-identical to upstream stable
[`v7.2.11` at `a2de78ee6ed8bf8cd99d177c74865a6e8c3cb6ea`](https://github.com/gregkh/linux/blob/a2de78ee6ed8bf8cd99d177c74865a6e8c3cb6ea/drivers/gpu/drm/scheduler/sched_rq.c):
SHA-256 `634142eb99ad9a0110cb1c2fcd5b3944a3548dd05422f19730c30939242480d6`.
The drm-misc-next `sched_entity.c` SHA-256 is
`e6183847bb4bb2598fe70934113d2e9a25ec9d1c2e54620f792b0e695ad4ba2a`.
See the [source manifest](novelty-baselines-sources.json) for source pins.

This is **not a hardware reproduction on drm-misc-next**. Arch's complete
downstream source/config and capture module hashes have not been reconstructed.
The already merged push/pop locking fix
[`2ab510e63197`](https://github.com/torvalds/linux/commit/2ab510e63197360945f915dd5631a77c63ac6b27)
is present in upstream v7.2.11 via
[`e89900c441fe`](https://github.com/gregkh/linux/commit/e89900c441fec90644baf49c3f46ce76d91c51e4);
it leaves the successor-only accounting branch below intact. This report does
not attribute the observation to that race or request reapplying its fix.

## Reproduction recipe

The [artifact repository](https://github.com/jinhobh/drm-gpu-scheduling) contains
the burst client, capture scripts and analysis. Use a Hyprland session with one
active 60 Hz output, DPM auto, the indicated policy and two hardware submissions.
Leave other workloads/input idle. Preserve the stock kernel and default boot
entry. The commands below use the original checkout's absolute path; substitute
the checkout prefix on another machine.

1. Build the victim (needs C compiler, glslc, Vulkan/Wayland development files,
   wayland-protocols and wayland-scanner):

   ```sh
   make -C /home/jinho/Projects/drm-gpu-scheduling/trace/burst_client
   ```

2. For this machine, `sched_policy` is read-only and amdgpu is in the initramfs.
   Use a one-off boot command line `gpu_sched.sched_policy=2`, then verify
   `/sys/module/gpu_sched/parameters/sched_policy` is `2`. Verify
   `hyprctl monitors -j` lists only eDP-1 and
   `/sys/module/amdgpu/parameters/sched_hw_submission` is `2`.

3. From the logged-in graphical user's terminal, run the same cell three times
   (trace-cmd, bpftrace and Python are required):

   ```sh
   sudo DPMS=auto HOGS=vkmark-cube REPS=3 /home/jinho/Projects/drm-gpu-scheduling/trace/matrix.sh fair-reentry-repro
   ```

   This also records one idle calibration. The actual contention workload is
   `burst.sh vkmark-cube --iters 736 -n 10 -i 300 -b 20`: 20 bursts of 10 frames,
   300 ms idle between bursts, 3 s flood warmup. The hog command is
   `vkmark --run-forever -b cube -p mailbox --size 1280x1000` with the default
   requested size; Hyprland tiled it to the original single-output workspace
   (about 1904×1163 effective geometry). The harness switches to an empty workspace and
   restores it afterward. Check recorded geometry and that vkmark survived.
   The matrix restores DPM auto and GFXOFF on exit.

4. Join and inspect from any directory:

   ```sh
   python3 /home/jinho/Projects/drm-gpu-scheduling/trace/join.py /home/jinho/Projects/drm-gpu-scheduling/traces/fair-reentry-repro/*/*/
   python3 /home/jinho/Projects/drm-gpu-scheduling/trace/fair_sleepers.py /home/jinho/Projects/drm-gpu-scheduling/traces/fair-reentry-repro/auto-vkmark-cube-r*/*/
   ```

   Compare frame-0 start latency and intervening vkmark dispatches. Repeat on
   FIFO (`gpu_sched.sched_policy=1`) under a different capture name as a control;
   RR (`0`) is optional. A new control should use matched sessions and outputs.
   The commands are a recipe; no fresh root captures were performed for this draft.

## Existing capture evidence

The read-only command
`python3 trace/fair_sleepers.py traces/m3-p2/auto-vkmark-cube-r*/*/`
was rerun on 2026-11-04. Full compact output is in
[fair-starvation-evidence.txt](fair-starvation-evidence.txt); input paths, sizes
and SHA-256 hashes are in [fair-starvation-inputs.json](fair-starvation-inputs.json).

| Capture under `traces/m3-p2/` | Frame-0 waits ≥ 5 ms | Overtaking vkmark pops | Empty after pop |
|---|---:|---:|---:|
| `auto-vkmark-cube-r1/20261019-142821` | 6/20 | 1,503 | 1,503 |
| `auto-vkmark-cube-r2/20261019-142843` | 9/20 | 2,149 | 2,149 |
| `auto-vkmark-cube-r3/20261019-142529` | 6/20 | 1,468 | 1,468 |
| Total | 21/60 | 5,120 | 5,120 |

For example, r2 burst 9 waited 15.469 ms with 325 intervening vkmark pops;
r3 burst 13 waited 15.528 ms with 330. Every one of the 5,120 intervening pops
has no successor queued by its following run tracepoint. We infer queue
exhaustion from per-context sequence/timestamps, not a probe inside the pop:
a successor arriving between pop and run could make an empty queue appear
nonempty. The count is conservative under that approximation.

All ten waits above 15 ms end with no queued vkmark work. The next HIGH
Hyprland submission follows the victim run by 0.048–0.191 ms, consistent with
a refresh-periodic submission gap. The traces do not show whether CPU scheduling,
swapchain waits or something else caused that gap. **15 ms is not a scheduler
bound.** A wait longer than the observed maximum remains possible in principle.

Historical FIFO/RR controls had no ≥ 5 ms frame-0 stalls among 100 frames in
total; see the [m3 comparison in plan.md](../../plan.md). These were different
boots, and later FIFO p1r also had a second output active. They support a
follow-up controlled reproduction, not an isolated estimate of policy effect.
This report concerns GPU start delay, not a demonstrated presentation-latency
improvement, indefinite starvation, or independent-session statistical significance.

## Source-supported mechanism and remaining uncertainty

In both pinned `sched_rq.c` versions, `drm_sched_rq_pop_entity()` calls
`drm_sched_entity_get_job_ts()` (and therefore
`drm_sched_entity_update_vruntime()`) only if `next_job` exists. That update
folds `runtime - prev_runtime`, weighted by priority, into the entity's tree
key. If the queue emptied instead, it removes the entity and saves its
relative vruntime lag without doing that runtime conversion.

`drm_sched_entity_stats_job_add_gpu_time()` still accumulates runtime and the
job-duration EWMA through delayed free-worker accounting. Thus the runtime is
not erased; its conversion into vruntime can be deferred repeatedly. On
re-entry, zero saved lag can receive favorable placement relative to the head:
for equal priority and entity EWMA ≤ scheduler EWMA, one nanosecond ahead.
Repeated empty-queue re-entry can therefore overtake a waiting victim while
avoiding the successor-path charge to its tree key. Software-queue exhaustion
does not imply hardware idleness; two jobs can remain in flight.

The captures record the necessary repeated queue-exhaustion pattern, not the
internal `runtime`, `prev_runtime`, `vruntime`, EWMAs or exact free-worker order.
They cannot prove the precise restore branch on every insertion. A next
diagnostic should record those values around add/pop/save/restore and delayed
runtime accounting, together with queue occupancy and dispatch/completion,
to test this explanation and any proposed repair. No patch is proposed here;
charging before save alone requires review of relative debt, delayed accounting,
locking and fairness. The withdrawn min_vruntime diff is not a validated fix.

## Review and submission handoff

The compact evidence and hashes are committed; raw `trace.dat`, `bpf.txt`,
`burst.csv`, `frames.csv` and metadata remain local under the paths above and
are gitignored. They are not downloadable from the repository. Before sending,
prepare a reviewed, shareable trace bundle or arrange a private transfer;
retain originals matching the manifest. Do not claim raw traces are attached.

Recheck drm-misc-next immediately before sending and update the source pins if
it moved. If available, reproduce on that pinned build and retain downstream
patch/config/module provenance. Resolve recipient addresses from that tree's
`scripts/get_maintainer.pl` and the existing dri-devel thread; no recipients have
been contacted. Sending is a separate user action. The full local source and
trace discussion is in [fair-sleepers.md](fair-sleepers.md).
