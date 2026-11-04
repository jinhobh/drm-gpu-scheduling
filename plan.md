# plan.md — phase 2: real traces, real hardware

Read this first. It says what has been done, what is decided, and what to do next.
Update the **Status** and **Next** sections whenever a step finishes.

## Status (2026-11-04)

**Done: phase 1, the policy search in the simulator.**
- `gpusim/` is a frozen, deterministic drm_sched + amdgpu APU simulator; `score.py` scores
  `policy.py` on 10 scenarios × 5 seeds (`jank_score`, lower is better). See README.md.
- The agent loop (`program.md`) ran on branches `opt/oct16` and `opt/oct18`: 60 experiments,
  32 kept (`results.tsv`, untracked). Dev `jank_score` went from **59.9** (upstream-fair
  approximation) to **16.26** (`policy.py` at `da6958c`); FIFO is 88.0, the cheating oracle 37.7.
- The policy: fair vruntime with bounded sleeper credit (BORE-style), per-frame GPU cost to tell
  hogs from interactive clients, at most one NORMAL/LOW job on the ring, compositor-grid
  prediction from fence deadlines, budgeted holds so a hog job does not run over an urgent frame,
  and starvation aging. Full description: the docstring at the top of `policy.py`.
- The search has reached diminishing returns. **Do not tune `policy.py` further** until phase 2
  (fitting the sim) says to.

**In progress: real traces on real hardware** (phase numbers below are this plan's).
- Phase 0 (go/no-go checks): **done, GO.** All GL and Vulkan rendering goes through drm_sched
  (no user queues); Hyprland renders at HIGH priority and is the only source of fence deadlines;
  DPM can be pinned; `gpu_metrics` gives averaged clocks every ~6 ms.
- Phase 1 (attribution study): capture harness and per-job latency breakdown done
  (steps 1.1, 1.2 jobs). The synthetic burst client is done (1.3a) and gave first no-root
  results (DPM `auto`, FIFO, below). Hogs picked (1.4). **First traced matrix `m1` done**
  (39 runs, FIFO, eDP-1 only; 1.5 below), analysed from the trace alone because a capture bug
  lost every burst.csv (fixed). Main results: (1) the burst delay behind a heavy hog is ~all
  queueing behind the hog's *two in-flight* non-preemptible jobs (credit_limit 2), not FIFO's
  choice (`pick` ≈ 0.02 ms) and not DPM; (2) a hog limited to one in-flight job cuts it from
  13–17 to 2–7 ms for −5% hog fps, i.e. the policy's one-NORMAL-job-on-the-ring rule is the
  lever; (3) GFXOFF exit adds ~1.7 ms to the first job after ≥ 100 ms idle, DPM does not.
  **`m2` done** (32 runs, with burst.csv): the per-frame join is validated (every frame
  matched, gpu_start inside [t_run, t_done], `wake` −0.02 ms) and confirms all three with
  per-frame data: vk-hog 11.2–11.5 ms vs vk-hog1 3.6–4.9 ms start latency, hog fps
  unchanged (130–131 both, −1% GPU busy); GFXOFF exit is ~1.0 ms *before* the first
  timestamp plus ~0.4 ms slower exec. New: under any load (even vkmark, 0.26 ms GPU wait)
  frames present one refresh later (p50 50 vs 33 ms) — compositor pacing, not GPU
  scheduling; GPU scheduling shows in frame 0 and the p99 tail. m2 also exposed a layout
  confound (tiled windows changed frame costs), now fixed. **`m3` done 2026-10-19**
  (`sched_policy` 1/2/0, 1.5): ordering doesn't fix bursts behind a hog (`pick` ≤ 0.2 ms,
  vk-hog p99 ~17 ms under all three; 1 in flight halves it under all three); fair helps a
  hog-blocked frame 0 (8.4 vs 13–14 ms) but starves 35% of after-idle frames behind
  vkmark's job flood for up to 15.5 ms. The p1 re-run (`m3-p1r`, 2026-10-20) agrees; a
  ~1 ms FIFO-vs-others gap remains, confounded with boot age and p1r output count (Hyprland renders more and
  costlier jobs in long-running boots; 1.5).
- **Upstream fair sleeper study done (2026-11-04, former Next 1).** Read pinned
  Linux `v7.2.11` scheduler sources and checked all three m3-p2 vkmark captures.
  Fair only folds cumulative runtime into vruntime on a pop with a successor still
  queued. A flood that repeatedly empties its software queue can rejoin ahead of
  the waiting victim using the short-job heuristic without charging that runtime
  to its tree key. All 5,120 overtaking vkmark pops in the 21 frame-0 waits ≥ 5 ms
  had empty queues after pop. **There is no 15 ms scheduler bound**: all ten waits
  > 15 ms end with no queued vkmark job and a HIGH Hyprland submission 0.048–0.191 ms
  later, consistent with a refresh-periodic submission gap. The cause of that CPU
  gap and exact internal vruntime branches are not recorded. Policy 3 needs
  persistent accounting, bounded sleeper credit, aging, and the in-flight limit;
  phase 2 needs the actual upstream accounting path, not completion-time updates.
  See [the source/evidence report](trace/reports/fair-sleepers.md) and
  `trace/fair_sleepers.py`. Frozen simulator/policies unchanged.
- **Research direction updated (2026-11-04):** pursue a conference contribution on
  automatic control of in-flight GPU work for interactive responsiveness. The measured
  latency reduction justifies further work, but the proposed kernel policy is not yet
  hardware-validated and novelty remains to be established. A workshop/short measurement
  paper is a nearer milestone; a full systems paper requires the comparisons and evidence
  below. This is a research target, not a claim of publication readiness.
- **Compositor pacing trace/source study done (2026-11-04, former Next 1).**
  Added `trace/pacing.py` and [the report](trace/reports/compositor-pacing.md).
  In m2, frame callback → submit is only 0.04–0.07 ms and present call → next
  Hyprland GPU submission is ~16.5 ms both alone and under vkmark. The extra
  refresh appears in next-submission → presentation (~17 vs ~34 ms), which is
  not a surface-latch measurement. It is **not universal**: m3-p2 vkmark is
  ~34 ms callback → presentation in all three runs; m3-p0 is ~34/50/34 ms.
  The old “50 ms under any load/all policies” statement was too broad. Exact
  WSI/server commit/latch timing remains unobserved; do not fit a fixed extra
  refresh under load. Hyprland v0.56.0 has no render-ahead knobs; its optional
  new render scheduler defaults off, but capture-time values were not recorded.
  **Monitor confound:** all 16 m3-p1r metadata files list eDP-1 AND DP-1 active;
  earlier m2/m3 matrices list only eDP-1. p1r is not a single-display repeat and
  cannot isolate boot age. Frozen simulator/policies unchanged.
- Phases 2–4: not started.

**Tooling (all in `trace/`, captures in `traces/`, gitignored):**
| file | what it does |
|---|---|
| `capture.sh` | root: `sudo trace/capture.sh NAME [SECONDS] -- WORKLOAD...` records trace-cmd (`-C mono`), bpftrace (`bpf/ids.bt`), fdinfo (`clients.py`) and `gpu_metrics.py` while running WORKLOAD as the user in the Hyprland session; writes `traces/NAME/<timestamp>/` |
| `parse.py` | `python3 trace/parse.py RUN`: per-job breakdown to `RUN/jobs.csv` + per-client p50/p99/max table |
| `gonogo.py` | which GPU clients go through drm_sched, their priority, who sets deadlines |
| `bpf/ids.bt` | kprobes: job → pid/comm/priority/client_id; fence deadlines |
| `clients.py` | fdinfo snapshot of amdgpu clients (engine time, memory, doorbells) |
| `gpu_metrics.py` | gpu_metrics v3.0 sampler (gfxclk, socclk, fclk, uclk, activity, power) |
| `dpm.sh` | show / set `power_dpm_force_performance_level` (root to set) |
| `workloads/gonogo.sh` | step 0.2 workload: vkmark + mpv + Hyprland swaps + kitty launch |
| `burst_client/` | ground-truth burst source (C, Vulkan + Wayland), `make -C trace/burst_client`; per-frame CSV log (submit, GPU start/end, presented). Usage: `burst_client --help`, doc at the top of `main.c` |
| `burst_stats.py` | `python3 trace/burst_stats.py LOG_OR_RUN [--by-idle]`: per-frame-index latencies of a burst_client log, or after-idle vs warm exec (calibration) |
| `workloads/hog.sh` | `hog.sh NAME`: one background load (`glmark2-terrain`, `glmark2-refract`, `stk`, `vk-hog`, `vk-hog1` (1 job in flight), `gl-hog` (glmark2 terrain off-screen 4K, 8.5 ms), `vkmark-cube`, `none`) |
| `workloads/burst.sh` | `burst.sh HOG [BURST_CLIENT_ARGS]`: switch to an empty Hyprland workspace (back at exit), probe GPU timestamps, start the hog, warm up 3 s, run burst_client; logs to `$CAPTURE_OUT` (set by `capture.sh`) |
| `join.py` | `python3 trace/join.py RUN...`: 1.2b, burst.csv × trace → `RUN/frames.csv`, per frame: submit / dep / order / pick / credit / ring / wake split of the start latency, `ok` (gpu_start inside [t_run, t_done]), `busy_by` (whose jobs ran on the ring while the frame waited); medians for frame 0 vs 1+, one line per run, pooled per condition; `--trace-only` (automatic without burst.csv) takes the frames from the trace alone; a run whose timestamp correction is looser than 100 µs is re-aligned on the fences (stderr says by how much) |
| `pacing.py` | `python3 trace/pacing.py RUN... [--csv FILE]`: read-only frame callback / present-call / Hyprland gfx submission / hardware vblank comparison; selects output by presentation MSC and timestamp; see `trace/reports/compositor-pacing.md` |
| `fair_sleepers.py` | `python3 trace/fair_sleepers.py RUN...`: read-only check of frame-0 stalls, overtaking vkmark queue exhaustion, queued work at release, and nearest HIGH Hyprland submission; see `trace/reports/fair-sleepers.md` |
| `compare.py` | `python3 trace/compare.py [LABEL=]MATRIX[,MATRIX]...`: after join.py, per hog × matrix: start latency f0/f1+ p50/p99, presented − intended, Hyprland ms/frame, hog fps and job time |
| `matrix.sh` | root: `sudo trace/matrix.sh [NAME]`: traced `burst.sh` runs for each hog × DPM condition × `REPS`, plus a `--calib` run per condition, shuffled; restores DPM auto / GFXOFF on exit; rejects unknown `HOGS` names up front; `DRY=1` prints the order |

Captures so far: `traces/gonogo/20261018-031904` (GL only, vkmark crashed),
`traces/gonogo/20261018-032454` (GL + Vulkan, vkmark cube). Both: FIFO, DPM auto, two displays.
No-root burst_client logs (no kernel trace): `traces/burst-noroot/20261018-survey/<hog>/` and
`traces/burst-noroot/20261018-calib-auto/` (FIFO, DPM auto, window on DP-1 at 75 Hz, tiled
next to the terminal).

**Machine facts (checked 2026-10-18):**
- Ryzen AI 7 350 / Radeon 860M (GC 11.5.2, MES present), 512 MiB carveout, Hyprland 0.56 (Wayland),
  Mesa 26.2.5.
- Kernel **7.2.11-arch1-1**. `gpu_sched` is a module.
  `sched_policy`: 0 = RR, 1 = FIFO (current), **2 = upstream "Fair (experimental)"**, so our
  policy will be `sched_policy=3`.
- `amdgpu.user_queue=-1` (auto), but no client uses user queues: all rendering goes through
  drm_sched (step 0.2).
- `pp_dpm_sclk` shows min / current / max (600 / ~700 idle / 3000 MHz),
  `power_dpm_force_performance_level=auto`.
- Two displays: eDP-1 1920x1200 @ 60 Hz, DP-1 2560x1440 @ 75 Hz (see risks). DP-1 (a dock)
  unplugged 2026-10-18 19:01 for the traced matrix: eDP-1 only in m1/m2 and initial m3;
  both outputs active in m3-p1r (capture metadata).
- Hyprland 0.56 has a **Lua config**: `hyprctl dispatch` takes Lua, e.g.
  `hyprctl dispatch 'hl.dsp.focus({ workspace = "empty" })'` (the old
  `hyprctl dispatch workspace N` is a Lua syntax error). Boot: systemd-boot + UKI
  (`/boot/EFI/Linux/arch-linux.efi`), Secure Boot off, amdgpu in the initramfs (`kms` hook).
- Installed: linux-headers, trace-cmd, perf, bpftrace, amdgpu_top, vkmark, glmark2,
  supertuxkart, mpv, ydotool, wtype, vulkan-radeon (RADV); gcc, clang, glslc, wayland-scanner,
  wayland-protocols, vulkan-headers, numpy. Not installed: xonotic, meson/ninja, vulkan-tools.
- tracefs/debugfs need root and sudo needs a password. The agent cannot sudo: ask the user to
  run root commands, **with absolute paths** (the user's terminal is often in another
  directory), e.g. `sudo /home/jinho/Projects/drm-gpu-scheduling/trace/capture.sh ...`.

**Decisions (from the user):**
- Scope: the full pipeline (phases 0–4 below).
- **Primary claim:** interactive *bursts* (animations, app launches, first frames after input)
  suffer measurable GPU delay, **and that delay is caused by GPU scheduling**, not by the CPU,
  DPM clock ramp-up, or the compositor. Games and mpv video are comparison workloads.
- Background hog: a second game or benchmark, free and scriptable (vkmark, glmark2,
  supertuxkart timedemo). No paid games.
- Hyprland only.
- **This is the user's daily laptop.** Kernel changes go only into a separately named
  side-by-side test kernel with its own boot entry. Never modify the stock kernel, its modules
  (`/lib/modules/7.2.11-arch1-1`), or the default boot entry.
- A synthetic burst client is kept as the ground-truth control (exact frame cost and timing).

## Research direction and publication gates (2026-11-04)

**Candidate contribution:** a kernel scheduler that controls in-flight background GPU work
and sleeper placement to improve real interactive presentation latency while retaining
throughput, fairness, and starvation protection. The decisive question is whether the full
policy earns its complexity compared with a simple global one-job limit.

**Evidence so far:** reducing a cooperating hog from two jobs in flight to one roughly
halves typical GPU start latency in m3 (~14–15 to ~5–6 ms), at ~5% hog throughput cost.
This is a client-side intervention, not a hardware result for `sched_policy=3`. The
simulator's 59.9 → 16.26 development score (~73% reduction) is a custom simulation metric,
not a measured desktop responsiveness improvement. Existing measurements use one laptop,
one compositor, mostly synthetic victims and typically three run repetitions; frame
counts alone do not establish statistical significance across independent sessions.

**Novelty must be checked before expanding the design.** Queue-depth limiting and GPU
interference protection have prior art. Start with Linux's
[V3D queue-depth rationale](https://docs.kernel.org/gpu/v3d.html),
[TimeGraph](https://www.usenix.org/conference/usenixatc11/timegraph-gpu-scheduling-real-time-multi-tasking-environments),
and the [upstream fair vruntime fixes discussion](https://mail-archive.com/dri-devel@lists.freedesktop.org/msg635635.html).
These are starting points, not a completed related-work survey. Separate the empty-pop
accounting mechanism in our report from known upstream races/fixes, and pin the exact
commits tested. A regression in an old fair version is insufficient as the main novelty.

**Required comparisons:** FIFO, upstream fair with applicable fixes, a simple global
one-job-in-flight limit, and the full policy. Keep stock fair as a historical baseline
where useful. A cooperating `vk-hog1` alone is not the global-limit baseline: the latter
must enforce its limit in the kernel without client changes, with treatment of HIGH and
KERNEL work documented. Use the same kernel base and settings for causal comparisons.

**Full-paper gates:**
- [ ] Demonstrate the kernel policy on unmodified real victims: application launches,
  scrolling and animations, with heavy GL/Vulkan loads, tiny-job floods and video controls.
- [ ] Report first-frame presentation latency and missed refreshes as primary outcomes,
  alongside GPU start latency, hog throughput, fairness, starvation and overhead. Resolve
  compositor pacing sufficiently to distinguish GPU gains from presentation gains.
- [ ] Ablate in-flight limiting, sleeper credit/aging, hog classification and compositor
  prediction/holds. Establish which mechanisms improve the latency–throughput tradeoff
  beyond the simple limit; remove complexity that does not earn its place.
- [ ] Use independent run/session repetitions, randomized or counterbalanced policy order,
  controlled boot/session state and confidence intervals. Choose repetition counts for
  the precision needed; do not treat correlated frames as independent runs or draw strong
  p99 claims from 60 first frames. Hold out workloads used to evaluate the final policy.
- [ ] Seek a second hardware configuration, ideally another GPU generation, while retaining
  Hyprland scope. If unavailable, state the single-machine limitation and narrow the claim.
- [ ] Release scripts, pinned source/patches, workload settings, analysis and shareable trace
  data sufficient to reproduce the paper's figures.

**Submission decision:** first complete a reproducible measurement study and assess fit for
a workshop/short paper. Pursue a full systems submission once the kernel policy shows a
defensible benefit over the simple limit on real presentation outcomes, with the gates
above addressed. If the simple limit explains the gains, publish that narrower result
with appropriate novelty and scope rather than claiming the full policy is necessary.

## Next (to-do, in order)

Root steps are for the user; give commands with absolute paths.

1. **Novelty and baseline specification (no root, the agent).** Review the related work
   above and current upstream fair fixes; record exact commits, distinguish known bugs
   from our mechanism, and specify the kernel-enforced global one-job baseline and its
   priority treatment. This determines the minimum policy and hardware comparisons needed.
2. **Real application and measurement study:** real burst sources (1.3b), mpv as a video
   victim/load (1.4), and the CPU-load control (1.5), then the report (1.6). Include
   presentation outcomes and an independent-run statistical plan from the publication gates.
   Finish this study before deciding whether to submit a workshop/short paper.
3. **Targeted pacing follow-up:** the existing-trace investigation is complete; see
   `trace/reports/compositor-pacing.md`. Before attributing presentation changes to
   scheduling, record the exact Hyprland/Mesa builds and render options, verify a single
   active output, and trace actual Wayland commit/receipt/latch plus buffer/fence readiness.
   Compare idle/vkmark with matched geometry and repeated independent sessions. The present
   API call is not the actual commit timestamp. This needs new capture instrumentation;
   the existing traces cannot resolve which buffer the next Hyprland job sampled.
4. Phase 2 inputs found so far: GFXOFF exit (~1.0 ms before the command buffer starts plus
   ~0.4 ms slower first job, after ≥ 100 ms idle; the sim has no GFXOFF model), Hyprland job
   cost (p50 0.29 ms, eDP-1 only), credit_limit 2 as the binding mechanism, the compositor
   pacing above, and DPM auto's steady exec for light work (0.68–0.97 vs 0.62 ms at high).

5. **Optional: policy 1 right after a fresh boot (root, the user; ~7 min).** `m3-p1r`
   (2026-10-20) was meant to be this but ran in a 13 h old boot with 3 suspend/resumes, so
   the boot-age and second-display confounds in 1.5 are still open. If wanted: disable
   or unplug DP-1, `reboot` (no edit), log in, start nothing else, do not suspend,
   verify `hyprctl monitors -j` lists only eDP-1,
   check `cat /sys/module/gpu_sched/parameters/sched_policy` is 1, then
   ```
   sudo DPMS=auto HOGS="none vk-hog vk-hog1 gl-hog vkmark-cube" /home/jinho/Projects/drm-gpu-scheduling/trace/matrix.sh m3-p1f
   ```
   and `python3 trace/join.py -q traces/m3-p1f/*/*/`, `python3 trace/compare.py
   p1f=traces/m3-p1f p1r=traces/m3-p1r p2=traces/m3-p2 p0=traces/m3-p0`. If p1f's none
   baseline is 0.16 ms with Hyprland at ~1.04 ms/job, session/setup state explains
   the gap; p1r alone cannot separate boot age from output count.
   - How sched_policy is switched (kept for phase 4): it is read-only at runtime (0444) and
     amdgpu is in the initramfs, so a one-off kernel command line edit in systemd-boot
     (Secure Boot off, so the UKI accepts it; nothing persists):
     `systemctl reboot --boot-loader-menu=30`, `e` on the entry, End, append
     ` gpu_sched.sched_policy=N`, Enter; check the sysfs value before running.
   - Hog names are checked by `matrix.sh` now (the first p1 run had typos, `traces/m3-p1-badhog/`
     is not data). The GPU timestamp offset can jump between runs (seen only in the 15 h
     boot of 2026-10-19): `join.py` re-aligns loose runs on the fences.

---

## Phase 0: setup and go/no-go checks (stock kernel)

- [x] 0.0 Install the tools (done by the user).
- [x] 0.1 Tracepoints and structs, read from the module binaries and `/sys/kernel/btf` (no root):
  - `gpu_scheduler`: `drm_sched_job_queue`, `drm_sched_job_run` (format: `dev, fence=ctx:seqno,
    ring, job count, hw job count, client_id`), `drm_sched_job_done` (`fence=ctx:seqno signaled`),
    `drm_sched_job_add_dep`, `drm_sched_job_unschedulable`. `fence` is the *finished* fence;
    `client_id` = fdinfo `drm-client-id`.
  - `amdgpu`: `amdgpu_cs_ioctl`, `amdgpu_sched_run_job` (`timeline, fence, ring_name, num_ibs`),
    `amdgpu_bo_move`, `amdgpu_vm_flush`, `amdgpu_ib_pipe_sync`, plus DC/DM events.
  - No drm_sched tracepoint carries the priority or deadline, so `trace/bpf/ids.bt` kprobes
    `drm_sched_entity_push_job` (pid, comm, `entity->priority`/`rq_priority`, `drm_client_id`)
    and `drm_sched_fence_set_deadline_finished` / `dma_fence_set_deadline`.
  - **7.2's drm_sched already has upstream's fair rework**: `drm_sched_entity` has `stats`
    (`runtime`, `vruntime`, `avg_job_us` EWMA), `rq_priority`, `rb_tree_node`,
    `oldest_job_waiting`; `drm_sched_rq` is an rb-tree with `head_prio`; `drm_sched_fence` has
    `deadline` and `drm_client_id`. The phase 3 port builds on this, not on the 7.0 design.
  - Capture tooling: `trace/capture.sh` (root; trace-cmd `-C mono` + bpftrace + fdinfo sampler,
    runs the workload as the user in the Hyprland session), `trace/clients.py`, `trace/bpf/ids.bt`,
    `trace/workloads/gonogo.sh`, `trace/gonogo.py`. Output in `traces/` (gitignored).
  - Hyprland's main DRM client already shows both `drm-engine-gfx` and `drm-engine-compute`
    time in fdinfo: it uses the compute ring too.
- [x] 0.2 **Go/no-go: does gfx work go through drm_sched? GO for both GL and Vulkan.**
  - **Vulkan (RADV)** run `traces/gonogo/20261018-032454` (after `pacman -S vulkan-radeon`):
    vkmark (347 452 jobs) and mpv (Vulkan now, submits from its `vo` thread) all go through
    drm_sched, 0 doorbells. No user queues on this kernel/Mesa with `user_queue=-1`.
  - `vkmark -b cube` is **not a GPU-time hog**: ~29 000 FPS, 0.03–0.05 ms jobs. It floods
    the queue with tiny jobs, and others barely waited (mpv/vo p99 1.7 ms, Hyprland p99
    0.05 ms). Phase 1 needs heavy scenes (vkmark/glmark2 heavier scenes at full screen,
    supertuxkart) and the job-flood case as a separate scenario.
  - Parsing `trace-cmd report` text takes ~10 s for a 27 MB / 15 s trace. OK for now.
  - **GL (radeonsi)** run `traces/gonogo/20261018-031904` (stock kernel, FIFO, `user_queue=-1`,
    both displays on): every GPU-busy client (Hyprland, mpv, ghostty, kitty, qs)
    appears in `drm_sched_job_queue/run/done`, with 0 doorbells in fdinfo. User queues are not
    used for GL on this kernel. 11 098 jobs in ~15 s.
    (Vulkan was not tested in that run: `vulkan-radeon` was missing, so vkmark crashed and mpv
    fell back to GL.)
  - Rings seen: `gfx_0.0.0` (all client rendering), `comp_1.x.y` (a few compute jobs from mpv,
    ghostty, kitty, Hyprland), `sdma0` (amdgpu kernel jobs).
  - amdgpu kernel jobs carry negative `client_id`s (u64 −1, −2, −3, −4, −8, −10) and are
    submitted in the client's context; mpv's software-decoded video caused ~4 000 sdma jobs with
    id −3 (likely VM updates for per-frame uploads). Treat them as their own class in the parser.
  - First look at waits (queue→run): mpv gfx jobs p50 0.37 ms, p99 4.2 ms, max 5.3 ms, with
    only Hyprland, mpv and terminals running. Includes dependency waits (6 283 `add_dep`, 3 171
    `unschedulable` events), so the parser must split dep wait from scheduling wait.
  - Hyprland gfx jobs: 754 in ~15 s, run→done p50 1.6 ms, max 4.9 ms (two displays, one at
    2560x1440). The sim's compositor (`base_us=700` + 250/surface) is probably too cheap.
  - Script bugs found and fixed: the fdinfo sampler ignored SIGINT (background job), so the
    capture hung and files stayed root-owned (now SIGTERM, `set +e` in cleanup). bpftrace
    `nsecs` is CLOCK_BOOTTIME (≈ 9 900 s ahead of mono after suspends), now `nsecs(monotonic)`.
- [x] 0.3 Compositor priority and deadlines (same run):
  - Hyprland's gfx context is **HIGH** (755 pushes prio 1/rq_prio 1); it also has 28 NORMAL and
    50 KERNEL-priority pushes (kernel jobs and a second context).
  - **Fence deadlines are set only on Hyprland's fences** (754 `drm_sched_fence_set_deadline_finished`,
    all client 6, from `kworker/u64:*`, i.e. the atomic-commit worker). Client fences never get
    deadlines, so `client_deadline_hints=False` in the sim is the right default. Deadline
    values are CLOCK_MONOTONIC ns.
  - The policy's assumptions hold on this machine: HIGH entity = compositor, vblank grid
    from its deadlines. But with two displays the deadlines interleave two grids
    (`drm_vblank_event` crtc 1: 940 events; crtc 0: 14, eDP likely in panel self-refresh).
- [x] 0.4 DPM and clock sampling.
  - `gpu_metrics` is format **v3.0**: it has only *averaged* clocks (`average_gfxclk_frequency`,
    no instantaneous gfxclk) and the SMU refreshes it every **~6 ms** (≈165 Hz), so ≥ 1 kHz is
    not possible. `trace/gpu_metrics.py` logs every refresh (gfxclk, socclk, fclk, uclk, gfx
    activity, gfx power) and `capture.sh` now records it as `gpu_metrics.csv`. Its
    `system_clock_counter` is CLOCK_BOOTTIME ns; rows also carry a CLOCK_MONOTONIC stamp.
  - Consequence: measure clock ramp-up by its *effect*: run a fixed calibration job from
    `burst_client` after idle vs warm, with DPM `auto` vs pinned `high` (the exec time
    difference is the ramp cost). gpu_metrics is supporting evidence only.
  - Idle: sclk sits at level 1 (≈ 670–790 MHz), gfxclk average ≈ 650–690 MHz.
  - `trace/dpm.sh [auto|high|...]` shows / sets the level. **Pin and restore work.** On this APU
    `pp_dpm_sclk` lists min / current / max: `high` raises the *min* to 3000 MHz
    (`0: 3000Mhz 1: 714Mhz * 2: 3000Mhz`), but the current clock still read ~714 MHz while idle,
    so the GPU may still power-gate (GFXOFF) or clock down when idle. Pinning may not remove the
    wake-up cost after idle: the calibration job must check this. If it doesn't, GFXOFF can be
    disabled for a control run via debugfs `amdgpu_gfxoff` (root). `auto` restores min 600 MHz.

## Phase 1: attribution study (main deliverable)

Goal: for every interactive frame, a latency breakdown, plus controlled interventions that
pin the excess delay on scheduling.

**Per-frame breakdown, from tracepoints:**
```
input → client CPU → CS ioctl → [dep wait] → [sched wait: queued, not yet selected]
      → [ring wait: handed to HW, behind a non-preemptible job] → [exec] → compositor latch → vblank
```
- scheduling delay = sched wait + ring wait. With `credit_limit=2`, HW start ≈
  max(run, the previous job's done) on the same ring; check this assumption against the trace.
- Compare exec time with the same frame on an idle GPU to separate clocks/bandwidth from queueing.

**Steps:**
- [x] 1.1 `trace/capture.sh` (done in phase 0; also records `gpu_metrics.csv`, waits for
  bpftrace to attach, ~3 s, before starting the workload).
- [~] 1.2 `trace/parse.py RUN`: writes `RUN/jobs.csv` and prints per-client p50/p99/max of
  `dep_wait`, `order_wait`, `sched_wait` (of which `credit_wait`), `ring_wait`, `exec`.
  Done for jobs. Two lessons from the gonogo traces:
  - **order_wait** is needed: an entity's jobs run in order, so a job can wait behind its own
    entity's blocked predecessor (mpv's sdma VM-update jobs waited up to 4.6 ms this way,
    which first looked like a 4 ms scheduler stall). No policy can remove it, so it must not
    be counted as scheduling delay.
  - Dependencies: `drm_sched_job_unschedulable` also reports fences not seen in `add_dep`;
    the parser uses both. The scheduled fence of a job is context `finished - 1`.
  - Scheduling-attributable delay = `sched_wait` + `ring_wait`. `sched_wait - credit_wait` is
    the policy choosing someone else (plus ~20 µs work-item latency); `credit_wait` and
    `ring_wait` are queueing behind other clients' jobs on the ring (2 credits, non-preemptive).
- [x] 1.2b Per-frame rows: `trace/join.py` (written 2026-10-18; validated on `m2` and `m3`:
  every frame matched, ok = 1.00; `--trace-only` used on all of `m1`). Matches each frame
  to the gfx job(s) its thread queued between `t_submit` and `t_submit_ret` (fallback: any
  job of the pid up to 2 ms later), uses `parse.py`'s breakdown, splits `sched_wait` into
  `pick` (the scheduler chose another job while credits were free) and `credit` (ring full),
  adds `wake` = gpu_start − t_hw and `busy_by` (per-client ring occupancy during the wait).
  `--trace-only` (automatic without burst.csv) takes the frames from the trace: the gfx jobs
  of the last-started burst_client pid, split into bursts at gaps > 100 ms, the 50-job
  timestamp probes dropped; start_lat is then queue → HW start and exec includes any wake.
- [x] 1.3a `trace/burst_client`, the ground-truth control (done 2026-10-18).
  - C, Vulkan (RADV) + Wayland xdg-shell + presentation-time, plain Makefile, glslc shaders.
    Idle `--idle-ms` (list, `--shuffle`), then `--frames` frames; one fragment pass per frame
    over a fixed 512×512 work area (`--work`), cost `--iters N` or `--cost-us` (calibrated warm
    at startup; 736 iters ≈ 1.73 ms on an idle GPU at DPM auto), `--first-mult` for a heavier
    first frame, `--calib` preset (idle 1…3000 ms × 5, 8-frame bursts), `--present mailbox
    --pace fifo -i 0` turns it into an uncapped hog of exact cost (`vk-hog`).
  - Per-frame CSV (CLOCK_MONOTONIC ns): `t_intended`, `t_start`, `t_acquired`, `t_submit`,
    `t_submit_ret`, `t_present`, `gpu_start`/`gpu_end` (TOP/BOTTOM_OF_PIPE timestamps),
    `presented`, `refresh_ns`, `msc`, `flags` or discarded; pid in the header, tid per row.
  - **Hyprland does not queue FIFO commits**: a FIFO burst that runs ahead (free swapchain
    images) has its commits replaced, so frames 1–2 of each burst were *discarded*. burst_client
    therefore paces like a toolkit frame clock: the next frame starts on the previous commit's
    `wl_surface.frame` callback (`--pace frame`, default; `t_intended` = callback arrival).
    Real GTK/Firefox bursts behave this way too.
  - **GPU timestamp quirk**: on this APU `vkCmdWriteTimestamp` values are offset from the
    `VK_KHR_calibrated_timestamps` DEVICE domain by a constant **+7927.39 ms** (calibrated
    CLOCK_MONOTONIC itself is right). burst_client measures the offset with 50 timestamp-only
    probes on an idle GPU (interval intersection, ±50–75 µs), checks it again at exit (drift
    < 15 µs per run), and caches it in `$XDG_RUNTIME_DIR/burst_client-ts-corr` because probes
    are loose while a hog runs; `burst.sh` probes before starting the hog. The join with the
    trace (Next, item 3) must confirm `gpu_start` lies inside `[t_run, t_done]`.
  - wp_presentation clock is CLOCK_MONOTONIC (checked at startup).
- [ ] 1.3b Real burst sources, driven by scripts (`ydotool`, `hyprctl dispatch`, `wtype`):
  GTK app launches, Firefox smooth scroll, Hyprland window/workspace animations (compositor
  work, HIGH: a separate case from client bursts).
- [x] 1.4 Background and comparison loads, `trace/workloads/hog.sh` (frame cost at 1280×1400,
  uncapped):
  - **Heavy hogs**: `glmark2-terrain` (GL, ~7.2 ms/frame), `glmark2-refract` (GL, ~7.0 ms),
    `vk-hog` (burst_client, Vulkan, exactly 8.0 ms, 114 fps; `HOG_COST_US` to change).
  - **All vkmark scenes are light** (cube 0.07, vertex 0.06, texture 0.10, shading-phong 0.12,
    desktop 0.17, effect2d 0.32 ms/frame): vkmark is only the job-flood case (`vkmark-cube`).
    glmark2 jellyfish/ideas/shading are light too (0.14–0.34 ms).
  - `stk` (supertuxkart AI race, `--profile-time`) runs 114 fps but is **CPU-bound**: gfx only
    ~30% busy (~2.6 ms GPU/frame). A game comparison load, not a heavy hog. It ignores SIGINT
    (`burst.sh` escalates to TERM/KILL).
  - Still to add: mpv on a local clip as a video victim/load.
- [~] First no-root results (burst_client logs only, no kernel trace; FIFO, DPM auto, 20
  bursts × 10 frames, 300 ms idle, 736 iters; `traces/burst-noroot/20261018-*`). Per frame:
  start_lat = gpu_start − t_submit, latency = presented − t_intended (ms, p50):
  | hog | start_lat f0 / f1+ | exec f0 / f1+ | latency f1+ | skipped vblanks (of 180) |
  |---|---|---|---|---|
  | none | 0.8 / 0.5–1.3 | 2.12 / 1.73 | 25–32 | 46 |
  | vk-hog (8 ms jobs) | 16.0 / 14–15.5 | 0.92 / 0.85 | 39–52 | 182 |
  | glmark2-terrain (~7 ms) | 8.1 / 6.2–7.8 | 0.69 / 0.70 | 27–39 | 72 |
  | vkmark-cube (flood) | 0.15 / 2.5–2.9 | 0.48 / 0.46 | 27–40 | 101 |
  | stk | 0.15 / 0.8–1.2 | 2.10 / 1.68 | 26 | 53 |
  - **Contention delays burst frames by about two hog jobs**: 14–16 ms behind the 8 ms vk-hog,
    6–8 ms behind terrain, i.e. consistent with the hog holding both ring credits under FIFO.
    The trace join must split this into sched wait vs ring wait.
  - A flood of tiny jobs (vkmark cube) still costs 2.5–2.9 ms per frame under FIFO.
  - **DPM is a large confounder in exec time, with the opposite sign**: the same frame takes
    1.73 ms on an otherwise idle GPU but 0.46–0.85 ms when a hog keeps the clocks up (DPM auto
    leaves light interactive work at low clocks, up to ~3.7× slower). So the "idle-GPU
    baseline" exec must be taken at the same DPM state, and DPM `high` runs are needed.
  - Frame 0 after idle: exec is bimodal, either warm (1.72 ms) or 2.1–2.3 ms, and the share
    of slow cases does not grow with the idle gap (1 ms … 3 s; `--calib`, 5 per gap). Not a
    simple clock ramp; could be other clients' work overlapping (terminal, Hyprland). Needs the
    DPM `high` run and the trace.
  - Even alone, frames are presented ~25 ms (≈2 refreshes at 75 Hz) after `t_intended` and
    ~25% of inter-frame gaps skip a vblank. That is the compositor path (`to_pres` ≈ 22 ms from
    GPU end), so it belongs in the baseline; look at Hyprland's own jobs in the trace.
- [~] 1.5 Experiment matrix, repeated runs in randomized order:
  | intervention | rules out |
  |---|---|
  | idle vs contended GPU, same scripted burst | frame inherently too heavy |
  | DPM `auto` vs pinned `high` | clock ramp-up |
  | equivalent CPU load, no GPU contention | CPU |
  | `sched_policy` 0 / 1 / 2 (needs a reboot or module param change) | if only the policy changes and the delay changes, the scheduler is the cause |
  | hog with 1 instead of 2 jobs in flight (`vk-hog1`) | isolates the in-flight depth, the policy's main lever |

  **`m1` (2026-10-18 19:04–19:19)**: FIFO (`sched_policy=1`), eDP-1 only @ 60 Hz,
  conditions DPM auto / high / high + GFXOFF off, hogs none / vk-hog / glmark2-terrain /
  vkmark-cube × 3 repeats, `--iters 736 -n 10 -i 300 -b 20`, plus one `--calib` per condition;
  `traces/m1/`. burst.csv was lost: `capture.sh` created the run directory as root and
  chowned it only at exit, so burst_client (run as the user) could not write its log, and it
  only found out at exit. Fixed: `capture.sh` chowns `$out` before the workload and returns
  its exit code; burst_client opens `-o` at startup. (Lesson: before handing a root script to
  the user, check every file the user-run workload writes into a root-created path.) So this
  is `join.py --trace-only`: per
  victim job, queue → HW start split, and trace exec (t_done − t_hw). Medians, ms, frames 1+
  (frame 0 in brackets where it differs), repeats pooled (per-run medians agree within
  ~0.5 ms except auto-terrain 3.7–5.4 and one high-vkmark run 0.55):
  | hog | cond | queue→HW | credit | ring | pick | exec | busy_by (hog, Hyprland) |
  |---|---|---|---|---|---|---|---|
  | none | auto | 0.52 | 0 | 0.50 | 0.02 | 0.74 [2.49] | –, 0.72 |
  | none | high | 0.14 | 0 | 0.12 | 0.02 | 0.65 [2.51] | –, 0.15 |
  | none | high, no GFXOFF | 0.16 | 0 | 0.14 | 0.02 | 0.65 [0.71] | –, 0.16 |
  | vk-hog (8 ms jobs) | auto | 11.4 | 10.8 | 0.28 | 0.04 | 0.74 | 11.1, 0.30 |
  | vk-hog | high | 11.7 | 11.2 | 0.25 | 0.02 | 0.76 | 11.2, 0.27 |
  | vk-hog | high, no GFXOFF | 11.7 | 11.3 | 0.25 | 0.02 | 0.77 | 11.3, 0.27 |
  | glmark2-terrain | auto | 4.3 | 3.9 | 0.29 | 0.02 | 0.59 | 4.0, 0.34 |
  | glmark2-terrain | high | 2.4 | 2.1 | 0.25 | 0.02 | 0.68 | 2.1, 0.26 |
  | vkmark-cube (flood) | auto / high | 0.27 | 0.20 | 0.05 | 0.01 | 0.60 | 0.12, 0.2 |
  - **The wait behind a heavy hog is the ring's two credits, not FIFO's choice.** While a
    frame waits, ~1 Hyprland (HIGH, queued earlier) job per frame runs ahead of it and
    almost no hog job (0.02/frame): the frame arrives with two hog jobs in flight, the
    first freed credit goes to Hyprland, and the frame runs when the second hog job ends
    (so it shows as `credit`, with only Hyprland's ~0.25 ms as `ring`). Wait ≈ residual of
    one hog job + one whole hog job ≈ 4 + 8 ms. `pick` ≈ 0.02 ms everywhere. DPM and
    GFXOFF do not change it.
  - **In-flight control (no-root, burst.csv only, `traces/burst-noroot/20261018-inflight/`,
    DPM auto, 15 bursts)**: vk-hog → vk-hog1 (`--inflight 1`): start_lat (gpu_start −
    t_submit) p50 frame 0 16.8 → 1.9 ms, frames 1–9 12.8–16.1 → 5.4–7.2 ms, p99 ~18–20 →
    ~9.5 ms (one hog job); hog 107.5 → 102.3 fps (−5%, GPU busy 96.5 → 94.2%). The
    one-NORMAL-job-on-the-ring rule should give this without the client's cooperation.
    Presentation latency was too noisy at 15 bursts to judge (Hyprland's own jobs also wait
    behind the hog); m2 has 3 × 20 bursts.
  - **GFXOFF, not DPM, is the frame-0 penalty**: frame-0 exec minus warm exec after ≥ 100 ms
    idle (none + calib runs; gaps 100 / 300 / 1000 / 3000 ms): auto 1.59 / 1.53 / 1.57 /
    1.63, high 1.82 / 1.87 / 1.81 / 2.17, high + GFXOFF off 0.06 / 0.07 / 0.07 / 0.08. Flat
    in the gap length above 100 ms; shorter gaps merge in the trace-only view (m2 calib with
    burst.csv will resolve 1–30 ms). The earlier "bimodal +0.4 ms" (two displays, DP-1
    active) was likely the same effect damped by the second display keeping the GPU awake.
  - DPM is a small confounder on eDP-1 only: warm exec 0.74 ms auto vs 0.65 ms high on an
    idle GPU (the 1.73 ms in the two-display survey is not reproduced). It barely changes
    waits except under terrain (4.3 → 2.4 ms: shorter hog jobs at high clocks).
  - vkmark-cube's flood costs little in queue → HW start (0.27 ms) but the survey saw 2.5–2.9
    ms submit → GPU start: resolved by m2, see below.

  **`m2` (2026-10-18 19:29–19:43)**: as m1 but DPM auto and high + GFXOFF off only, hogs none /
  vk-hog / vk-hog1 / glmark2-terrain / vkmark-cube × 3, with burst.csv (all 32 runs; one run
  183/200 presented). `traces/m2/`. Join validation: 100% of frames matched one gfx job,
  `ok` = 1.00 everywhere, `wake` (gpu_start − t_hw) = −0.01…−0.03 ms on warm frames (inside
  the ±25 µs timestamp correction): **the ts correction and the `t_hw = max(run, prev
  done)` model hold**, and `submit` (t_submit → queue) is only 0.02–0.05 ms. Medians, ms,
  repeats pooled:
  | hog | cond | start f0 / f1+ (p99) | credit f1+ | ring f1+ | exec f1+ | latency f0 / f1+ p50 (f1+ p99) | missed vbl/frame | hog fps |
  |---|---|---|---|---|---|---|---|---|
  | none | auto | 1.08 / 0.40 (1.06) | 0 | 0.37 | 0.97 | 15.0 / 33.5 (67) | 0.41 | – |
  | none | high, no GFXOFF | 0.10 / 0.13 (0.51) | 0 | 0.12 | 0.62 | 15.6 / 33.5 (67) | 0.43 | – |
  | vk-hog | auto | 12.7 / 12.8 (22.9) | 11.6 | 0.26 | 0.77 | 38.5 / 50.3 (100) | 0.97 | 130 |
  | vk-hog | high, no GFXOFF | 10.8 / 10.1 (15.3) | 9.4 | 0.26 | 0.76 | 36.8 / 50.2 (100) | 0.80 | 130 |
  | vk-hog1 | auto | 4.24 / 4.04 (7.65) | 3.3 | 0.26 | 0.75 | 28.5 / 50.3 (67) | 0.83 | 131 |
  | vk-hog1 | high, no GFXOFF | 4.44 / 4.41 (10.3) | 3.8 | 0.27 | 0.76 | 28.9 / 50.2 (67) | 0.83 | 130 |
  | glmark2-terrain | both | 2.5 / 2.4 (3.4) | 2.1 | 0.25 | 0.66 | 30 / 50 (67) | 0.78 | – |
  | vkmark-cube | both | 0.12 / 0.26 (0.49) | 0.19 | 0.05 | 0.58 | 32 / 50 (67) | 0.78 | – |
  - **In-flight depth, confirmed per frame**: clean runs vk-hog 11.2–11.5 ms vs vk-hog1
    3.6–4.9 ms start latency (≈ one hog job less; with one hog job in flight the second
    credit goes to Hyprland, so it still shows as `credit`). The hog keeps its throughput:
    130.0–131.4 fps with 2 in flight vs 130.0–131.6 with 1 (GPU busy 97.5 → 96.6%), same
    iters/exec (7.4 ms jobs). User-visible: frame 0 is presented ~10 ms earlier (p50 38.5 →
    28.5) and the f1+ p99 drops a refresh (100 → 67 ms).
  - Two vk-hog runs are layout outliers and inflate the pooled vk-hog rows: `auto-vk-hog-r2`
    (hog jobs 10.1 ms, start 16.5) and `high-nogfxoff-vk-hog-r2` (hog cut to 468×287, 3.0 ms
    jobs, start 5.8). **Layout confound**: Hyprland tiled the test windows next to the
    user's two terminals on workspace 4, and burst_client clips its 512×512 work area to the
    window, so costs varied with the layout (victim 468–472×512 in most runs, 512×512 in 5,
    468×283 in one). Fixed for m3: `burst.sh` runs on an empty workspace, burst_client sets
    min = max size so Hyprland floats it at a fixed size, `gl-hog` renders off-screen.
  - **GFXOFF exit, located**: on an idle GPU at DPM auto, frame 0 after 300 ms idle has
    `wake` 1.0 ms (before the command buffer's first timestamp) and exec 1.41 vs 0.97 ms
    warm; with GFXOFF off, `wake` 0.04 and exec unchanged. (m1: DPM high alone keeps the
    penalty.) GFXOFF never engages under load, so it only inflates the idle baseline.
  - **vkmark discrepancy resolved**: with burst.csv, submit → GPU start under vkmark is 0.26
    ms (p99 0.5), not the survey's 2.5–2.9 ms. The survey's number was specific to that
    setup (two displays, DP-1 75 Hz, 1280×1400 tiled windows), not hidden submit or wake time.
  - **glmark2-terrain is no longer a heavy hog**: windowed on eDP-1 it runs ~1.2–1.7 ms/frame
    (the 7.2 ms in 1.4 was measured with two displays), so its wait is ~2.4 ms (≈ 2 jobs).
    `gl-hog` (terrain off-screen at 3840×2160, 8.5 ms, 119 fps) replaces it as the heavy GL hog.
  - **m2 compositor/presentation pacing dominates steady presentation**: f1+ latency p50 is
    50 ms (3 refreshes) for every hog, including vkmark with 0.26 ms GPU wait, vs 33.5 ms
    alone; missed vblanks per frame ~0.8 vs ~0.4. GPU scheduling adds on top: the vk-hog
    p99 of 100 ms and frame 0's +10 ms. The later pacing study shows this is not
    universal across m3 runs; see `trace/reports/compositor-pacing.md` and Next 3.
  - DPM auto vs high for light work on an idle GPU: 0.97 vs 0.62 ms exec (m1: 0.74 vs 0.65;
    varies by run, 0.68–0.97). Under load clocks are up and exec is the same.
  - **`m3` (2026-10-19): `sched_policy` 1 (FIFO) / 2 (fair) / 0 (RR)**, DPM auto, GFXOFF on,
    fixed layout, 3 reps per hog: `traces/m3-p1` + `m3-p1b` (vk-hog1; policy 1 ran in a
    15 h old boot with 4 suspend/resumes), `m3-p2`, `m3-p0` (each in a fresh boot booted with
    `gpu_sched.sched_policy=N`). 1 failed run (p1 vkmark r2). Every frame matched, ok 1.00;
    the p1 hog runs needed the fence re-alignment, p2/p0/p1r did not. (In p1's boot the GPU
    timestamp offset jumped between runs, 390 031 → 404 381 ms over 6 min, stable within a
    run; under a hog burst_client's probe is loose and the cached idle probe failed its check
    by ~0.3 ms, so every hog run was off by a constant 2.7–6.9 ms. `join.py` re-aligns runs
    looser than 100 µs on the fences, median tail = 0.04 ms as in m2; afterwards ok = 1.00
    and wake −0.02 ms, which the alignment does not force.) Start latency (gpu_start − t_submit, ms), presented − intended
    (ms), from `trace/compare.py p1r=traces/m3-p1r p1=traces/m3-p1,traces/m3-p1b p2=traces/m3-p2
    p0=traces/m3-p0` (1r = `m3-p1r`, policy 1 again on 2026-10-20, 16/16 runs, no loose
    timestamp probes, in a 13 h old boot with 3 suspend/resumes before it):

    | hog | pol | f0 p50 / p99 | f1+ p50 / p99 | pres f0 p50 | pres f1+ p50 / p99 | hog fps |
    |---|---|---|---|---|---|---|
    | none | 1r | 1.57 / 2.06 | 0.26 / 1.68 | 12.4 | 32.3 / 50.3 | |
    | none | 1 | 1.54 / 1.97 | 0.42 / 1.89 | 12.4 | 32.2 / 50.1 | |
    | none | 2 | 1.57 / 1.97 | 0.16 / 1.35 | 13.0 | 32.3 / 50.6 | |
    | none | 0 | 1.56 / 1.97 | 0.16 / 1.34 | 12.9 | 32.3 / 50.6 | |
    | vkmark | 1r | 0.12 / 0.23 | 0.66 / 1.17 | 31.8 | 50.3 / 67.2 | |
    | vkmark | 1 | 0.09 / 0.15 | 1.04 / 1.31 | 31.4 | 33.9 / 50.5 | |
    | vkmark | 2 | **1.59 / 15.5** | 0.44 / 5.27 | 32.7 | 33.9 / 67.3 | |
    | vkmark | 0 | 0.10 / 0.66 | 0.38 / 1.98 | 32.7 | 34.0 / 84.0 | |
    | vk-hog1 | 1r | 5.97 / 9.25 | 6.11 / 9.80 | 28.3 | 49.0 / 67.0 | 110–111 |
    | vk-hog1 | 1 | 6.34 / 9.43 | 6.07 / 9.55 | 29.3 | 33.6 / 50.3 | 110–111 |
    | vk-hog1 | 2 | 4.42 / 8.59 | 5.46 / 9.23 | 27.4 | 48.7 / 67.1 | 112–113 |
    | vk-hog1 | 0 | 5.69 / 9.33 | 5.67 / 9.27 | 29.0 | 33.7 / 67.1 | 111–112 |
    | vk-hog | 1r | 13.9 / 18.7 | 14.1 / 17.8 | 38.1 | 65.5 / 99.4 | 114–126* |
    | vk-hog | 1 | 14.2 / 19.3 | 15.0 / 17.7 | 49.5 | 49.9 / 83.5 | 115–119 |
    | vk-hog | 2 | 8.44 / 18.3 | 12.5 / 17.1 | 37.4 | 50.3 / 99.5 | 115–135* |
    | vk-hog | 0 | 12.9 / 17.0 | 13.6 / 17.2 | 50.7 | 65.6 / 100 | 115–119 |
    | gl-hog | 1r | 11.5 / 24.4 | 13.6 / 29.7 | 46.2 | 49.6 / 83.4 | |
    | gl-hog | 1 | 11.1 / 29.0 | 14.6 / 28.8 | 43.4 | 49.5 / 67.7 | |
    | gl-hog | 2 | 11.2 / 27.3 | 12.4 / 27.3 | 45.6 | 49.6 / 76.6 | |
    | gl-hog | 0 | 9.17 / 24.9 | 11.3 / 24.4 | 39.3 | 49.6 / 83.6 | |

    (*one vk-hog run each in p2 and 1r had 6.9–7.1 ms hog jobs instead of 8.0.)
  - **The prediction holds: ordering does not fix bursts behind a heavy hog.** Under every
    policy `pick` is 0.03–0.19 ms and the wait is `credit` (the hog's 2 jobs in flight):
    vk-hog f1+ p99 17.1–17.7 ms under all three. vk-hog1 (1 in flight) halves it under all
    three (p50 5.5–6.1, p99 9.2–9.6) at ~5% hog fps (110–113 vs 115–119). The policies
    differ by ≤ 2.5 ms in p50, part of which is the boot (below). p1r reproduces p1
    (vk-hog f1+ 14.1 vs 15.0, p99 17.8 vs 17.7; vk-hog1 6.1 vs 6.1; gl-hog 13.6 vs 14.6).
  - **Fair helps frame 0 a little behind a hog**: vk-hog f0 p50 8.4 (fair) vs 12.9 (RR) vs
    14.2 (FIFO); f0 credit 2.2 vs 5.7 vs 9.1 ms. The woken victim gets the first free credit
    ahead of the hog's queued job. Steady frames and the p99 don't change.
  - **Fair starves an after-idle frame behind a job flood**: with vkmark (0.03–0.05 ms
    jobs), 21 of 60 frame-0s waited ≥ 5 ms, up to 15.53 ms (credit ~14 ms + pick ~1 ms,
    the ring busy with vkmark the whole time, max ≈ 15.5 ms in every run); 0 of 100 under FIFO/RR. f1+
    p99 5.3 vs 1.3–2.0 ms. A sleeper-placement effect of upstream's fair vruntime; the
    ~15 ms ceiling was initially suspected to be a code bound. **Source study
    (2026-11-04) rules that out:** repeated empty-queue pops bypass the runtime-to-
    vruntime update; short-job re-entry can repeatedly overtake a waiting frame.
    The ten > 15 ms waits all end at a flood submission gap near the next HIGH
    compositor submission. See [the report](trace/reports/fair-sleepers.md).
    (`fair_sleepers.py` recounts 21 ≥ 5 ms cases; the earlier manual count was 18.)
  - **Boot-age confound, not resolved by p1r**: p1r also ran in a long-running boot (13 h,
    3 suspend/resumes; the fresh boot asked for in Next did not happen), and shows the
    same pattern: Hyprland renders more and costlier gfx jobs than in the fresh p2/p0
    boots (none runs: p1r 26–31 jobs/s at 1.32 ms p50, p1 23–28/s at 1.24–1.50, p2/p0
    20–22/s at 1.04; under vk-hog 1.67 vs 1.31), and its share of the victim's wait is
    0.31–0.43 vs 0.16 ms/frame idle. Hyprland's per-job cost should not depend on the
    drm_sched policy, so session state is a possible cause. The pacing audit also found
    DP-1 active in every p1r metadata file: policy, boot age, and output count are confounded. It moves the
    FIFO numbers by ≤ ~1 ms and changes none of the conclusions above. Settling it needs
    eDP-1 alone and policy 1 right after a fresh boot (Next 5, optional).
  - Presentation (corrected by the 2026-11-04 pacing audit): vkmark f1+ p50 is
    ~34 ms in m3-p2 and two m3-p0 runs, ~50 ms in the other m3-p0 run and m3-p1r.
    Thus ~50 ms is not universal across loads/policies; see the pacing report and Next 3.
    The p99 differences (67–100 ms) are one refresh apart and noisy with 3 runs.
- [ ] 1.6 Measurement report: latency-breakdown and presentation plots per scenario,
  independent-run uncertainty, confounds, related-work comparison and reproducible artifact.
  Assess a workshop/short-paper submission using the research-direction gates above.

## Phase 2: fit the simulator (reuses the phase 1 traces)

- [ ] 2.1 Freeze the current sim as v1 (tag it) so old scores stay comparable.
- [ ] 2.2 Measure `DEFAULT_CFG` constants (`submit_us`, `ctx_switch_us`, `sched_wake_us`, SDMA
  bandwidth, GTT penalty) from the traces.
- [ ] 2.3 Fit client *models*, not timestamp replays (a FIFO trace bakes in FIFO's feedback):
  per-job cost distribution, jobs/frame, CPU think time, first-frame multiplier, burst shape.
- [ ] 2.4 Model measured GFXOFF wake-up and DPM effects using calibration execution times;
  use coarse `gpu_metrics` samples as supporting evidence, not an instantaneous ramp trace.
- [ ] 2.5 Rebuild `scenarios.py` around the real workloads (Hyprland animations, the benchmarks, mpv).
- [ ] 2.6 Validate sim-FIFO and sim-fair against matching hardware versions on wait-time and
  frame-time distributions. Model fair's actual delayed runtime accounting and empty-pop
  path; distinguish stock 7.2.11 from the fixed upstream baseline.
- [ ] 2.7 Re-score all baselines and `policy.py`. If the ranking FIFO > fair > policy holds,
  compare with a simple global one-job limit before phase 3. If the ranking flips or the
  margin collapses, revisit the mechanisms on the fitted sim first. Simulator ranking
  guides implementation; it does not establish the paper's hardware claim.

## Phase 3: side-by-side test kernel

- [ ] 3.1 Package `linux-drmsched`: Arch's 7.2.11 PKGBUILD with `pkgbase` renamed, its own
  boot entry; the stock kernel stays the default.
- [ ] 3.1b Implement the kernel-enforced global one-job baseline and applicable upstream
  fair fixes on the same test-kernel base. Pin patches and document credit/priority rules;
  retain the stock kernel and default boot entry untouched.
- [ ] 3.2 Port the selected policy from the fitted simulator to C as `sched_policy=3` in `drivers/gpu/drm/scheduler`: fixed-point
  instead of floats, hrtimer to re-run selection after a hold, per-entity state in
  `drm_sched_entity` (fine here because amdgpu is rebuilt in the same tree), hooks at
  push/run/free/entity_init and fence deadline.
- [ ] 3.3 Equivalence test: build the C selection logic as a shared library, drive it from gpusim
  via ctypes, and check it scores the same as `policy.py`.
- [ ] 3.4 Default `amdgpu.user_queue=0` on this kernel. Add tracepoints if phase 1 found gaps
  (e.g. actual HW start).
- [ ] 3.5 Soak test: GPU hang/timeout recovery, suspend/resume, a day of normal use.

## Phase 4: hardware A/B

- [ ] 4.1 Re-run the phase 1 matrix on the same test-kernel base: FIFO, fixed upstream fair,
  global one-job limit and full policy (stock fair/RR as additional historical controls).
  Include real application victims, independent repetitions, controlled session state,
  counterbalanced policy order and confidence intervals for paired comparisons.
- [ ] 4.1b Run the mechanism ablations specified above. Compare presentation latency and
  missed refreshes at matched throughput costs, not just a custom aggregate score.
- [ ] 4.1c Repeat key experiments on a second hardware configuration if available; otherwise
  explicitly limit generalization to the tested system.
- [ ] 4.2 Success: improve real first-frame presentation latency/missed refreshes and the
  latency–throughput tradeoff over the simple global limit and fixed fair. Also require
  burst sched wait + ring wait to shrink, while the hog keeps
  ≥ 90% of its frame rate, two hogs stay fair (Jain ≥ 0.95), a mimic gets ≤ 55% of the
  contended GPU, and no job waits > 250 ms (NORMAL) / 1 s (LOW).
- [ ] 4.3 Final report and conference submission decision against the publication gates;
  release the reproducible artifact and update README (kernel versions, patches, results).
  State supported claims and limitations; if only the simple limit is justified, report
  that result without attributing its gains to the full policy.

## Open questions and risks

- ~~User queues may carry most gfx work~~: resolved, they don't (0.2).
- ~~Hyprland may not be HIGH or set deadlines~~: resolved, it is and does (0.3).
- ~~Pinning DPM to `high` may not remove the after-idle penalty~~: resolved in `m1`, it
  doesn't; the penalty is GFXOFF exit (~1.7 ms on the first job after ≥ 100 ms idle) and
  disabling GFXOFF removes it. On eDP-1 alone DPM's steady effect is small (0.74 vs 0.65 ms).
- The sim's compositor cost looks too low (Hyprland frames 1.1–1.6 ms median, up to 4.9 ms);
  fit it in phase 2.
- DPM ramp-up may dominate burst latency: not under contention (`m1`: waits are the same at
  auto / high). GFXOFF exit only hits the first frame after idle on an *idle* GPU, so it
  inflates the uncontended baseline, not the contended case.
- ~~burst_client's window is tiled by Hyprland~~: it did change frame costs (m2, both
  victim and vk-hog); fixed from m3 on (empty workspace, fixed-size floating burst_client,
  off-screen `gl-hog`). Windowed glmark2/vkmark still take the workspace size (1904×1163).
- **Two displays** were connected until `m1`: eDP-1 1920x1200 @ 60 Hz and DP-1 2560x1440
  @ 75 Hz, i.e. two vblank grids. The policy predicts a single compositor grid. Experiments
  used eDP-1 only in m1/m2/initial m3, but all m3-p1r metadata files list DP-1 active
  again. Verify eDP-1 alone before future captures unless testing multi-monitor on purpose.
  `meta.txt` records the setup.
- Hyprland's own animations are compositor (HIGH) work, so they are not NORMAL bursts; client-side
  animations (GTK, Firefox, burst_client) are the NORMAL-burst case.
