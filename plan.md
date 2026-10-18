# plan.md — phase 2: real traces, real hardware

Read this first. It says what has been done, what is decided, and what to do next.
Update the **Status** and **Next** sections whenever a step finishes.

## Status (2026-10-18)

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

**Machine facts (checked 2026-10-18):**
- Ryzen AI 7 350 / Radeon 860M (GC 11.5.2, MES present), 512 MiB carveout, Hyprland 0.56 (Wayland),
  Mesa 26.2.5.
- Kernel **7.2.11-arch1-1** (README still says 7.0). `gpu_sched` is a module.
  `sched_policy`: 0 = RR, 1 = FIFO (current), **2 = upstream "Fair (experimental)"**, so our
  policy will be `sched_policy=3`.
- `amdgpu.user_queue=-1` (auto). User queues bypass drm_sched, so we must check which
  submissions actually go through it (step 0.2).
- DPM sclk levels 600 / 787 / 3000 MHz, `power_dpm_force_performance_level=auto`.
- Installed: linux-headers, trace-cmd, perf, bpftrace, amdgpu_top, vkmark, glmark2,
  supertuxkart, mpv, ydotool, wtype. **xonotic is not installed** (it is in the repos if wanted).
- tracefs/debugfs need root and sudo needs a password. The agent cannot sudo: ask the user to
  run root commands as `! sudo ...` in the Claude Code prompt, or to give tracefs group access.

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

## Next

**Phase 0, step 0.4 (user, root):** check that DPM can be pinned and restored:
```
sudo /home/jinho/Projects/drm-gpu-scheduling/trace/dpm.sh high
python3 trace/gpu_metrics.py | head -20      # gfxclk should read ~3000
sudo /home/jinho/Projects/drm-gpu-scheduling/trace/dpm.sh auto
```
Then **phase 1, step 1.2:** `trace/parse.py` (the per-job / per-frame breakdown), developed
against the two gonogo traces.

Note for the user: run root commands with absolute paths (or from the repo directory);
relative paths fail from other directories.

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
  - GL run `traces/gonogo/20261018-031904` (stock kernel, FIFO, `user_queue=-1`, both displays on):
  - **GO for OpenGL (radeonsi):** every GPU-busy client (Hyprland, mpv, ghostty, kitty, qs)
    appears in `drm_sched_job_queue/run/done`, with 0 doorbells in fdinfo. User queues are not
    used for GL on this kernel. 11 098 jobs in ~15 s.
  - (Vulkan was not tested in this run: `vulkan-radeon` was missing, vkmark crashed, mpv fell back to GL.)
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
- [~] 0.4 DPM and clock sampling.
  - `gpu_metrics` is format **v3.0**: it has only *averaged* clocks (`average_gfxclk_frequency`,
    no instantaneous gfxclk) and the SMU refreshes it every **~6 ms** (≈165 Hz), so ≥ 1 kHz is
    not possible. `trace/gpu_metrics.py` logs every refresh (gfxclk, socclk, fclk, uclk, gfx
    activity, gfx power) and `capture.sh` now records it as `gpu_metrics.csv`. Its
    `system_clock_counter` is CLOCK_BOOTTIME ns; rows also carry a CLOCK_MONOTONIC stamp.
  - Consequence: measure clock ramp-up by its *effect*: run a fixed calibration job from
    `burst_client` after idle vs warm, with DPM `auto` vs pinned `high` (the exec time
    difference is the ramp cost). gpu_metrics is supporting evidence only.
  - Idle: sclk sits at level 1 (≈ 670–790 MHz), gfxclk average ≈ 650–690 MHz.
  - `trace/dpm.sh [auto|high|...]` shows / sets the level. Pin and restore not yet tested (root).

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
- [ ] 1.1 `trace/capture.sh`: trace-cmd record of the phase-0 events plus `gpu_metrics` sclk
  samples, with a start marker written to `trace_marker`. Output goes to `traces/<scenario>/<run>/`
  (untracked).
- [ ] 1.2 `trace/parse.py`: trace → per-entity job table (pid/comm, ctx priority, queue, deps
  ready, run, HW start, done) and per-frame rows with the breakdown above.
- [ ] 1.3 Burst sources (primary), all driven by scripts (`ydotool`, `hyprctl dispatch`, `wtype`):
  Hyprland window/workspace animations, GTK app launches, Firefox smooth scroll, and
  `trace/burst_client` (small Vulkan/Wayland client with tunable per-frame cost and burst
  pattern, logging `wp_presentation` feedback) as the ground-truth control.
- [ ] 1.4 Background and comparison loads: none, vkmark, glmark2, supertuxkart timedemo, mpv
  (local clip). Games and video are also measured as victims.
- [ ] 1.5 Experiment matrix, repeated runs in randomized order:
  | intervention | rules out |
  |---|---|
  | idle vs contended GPU, same scripted burst | frame inherently too heavy |
  | DPM `auto` vs pinned `high` | clock ramp-up |
  | equivalent CPU load, no GPU contention | CPU |
  | `sched_policy` 0 / 1 / 2 (needs a reboot or module param change) | if only the policy changes and the delay changes, the scheduler is the cause |
- [ ] 1.6 Report: latency-breakdown plots per scenario (publish as an artifact page).

## Phase 2: fit the simulator (reuses the phase 1 traces)

- [ ] 2.1 Freeze the current sim as v1 (tag it) so old scores stay comparable.
- [ ] 2.2 Measure `DEFAULT_CFG` constants (`submit_us`, `ctx_switch_us`, `sched_wake_us`, SDMA
  bandwidth, GTT penalty) from the traces.
- [ ] 2.3 Fit client *models*, not timestamp replays (a FIFO trace bakes in FIFO's feedback):
  per-job cost distribution, jobs/frame, CPU think time, first-frame multiplier, burst shape.
- [ ] 2.4 Add a DPM clock-ramp model to `engine.py` from the `gpu_metrics` samples.
- [ ] 2.5 Rebuild `scenarios.py` around the real workloads (Hyprland animations, the benchmarks, mpv).
- [ ] 2.6 Validate: sim-FIFO vs real-FIFO (and sim-fair vs real `sched_policy=2`) on wait-time and
  frame-time distributions.
- [ ] 2.7 Re-score all baselines and `policy.py`. If the ranking FIFO > fair > policy holds,
  go to phase 3. If it flips or the margin collapses, re-run the agent loop (`program.md`)
  on the fitted sim first.

## Phase 3: side-by-side test kernel

- [ ] 3.1 Package `linux-drmsched`: Arch's 7.2.11 PKGBUILD with `pkgbase` renamed, its own
  boot entry; the stock kernel stays the default.
- [ ] 3.2 Port `policy.py` to C as `sched_policy=3` in `drivers/gpu/drm/scheduler`: fixed-point
  instead of floats, hrtimer to re-run selection after a hold, per-entity state in
  `drm_sched_entity` (fine here because amdgpu is rebuilt in the same tree), hooks at
  push/run/free/entity_init and fence deadline.
- [ ] 3.3 Equivalence test: build the C selection logic as a shared library, drive it from gpusim
  via ctypes, and check it scores the same as `policy.py`.
- [ ] 3.4 Default `amdgpu.user_queue=0` on this kernel. Add tracepoints if phase 1 found gaps
  (e.g. actual HW start).
- [ ] 3.5 Soak test: GPU hang/timeout recovery, suspend/resume, a day of normal use.

## Phase 4: hardware A/B

- [ ] 4.1 Re-run the phase 1 matrix with `sched_policy` 1 / 2 / 3 on the test kernel.
- [ ] 4.2 Success: burst sched wait + ring wait shrink with policy 3, while the hog keeps
  ≥ 90% of its frame rate, two hogs stay fair (Jain ≥ 0.95), a mimic gets ≤ 55% of the
  contended GPU, and no job waits > 250 ms (NORMAL) / 1 s (LOW).
- [ ] 4.3 Final report; update README (kernel version, `sched_policy=3`, results).

## Open questions and risks

- User queues may carry most gfx work on the stock kernel (step 0.2).
- Hyprland may not run at HIGH priority or set fence deadlines (step 0.3).
- DPM ramp-up may dominate burst latency; if so, that is a result too, and the policy may need
  to account for it.
- **Two displays** are connected: eDP-1 1920x1200 @ 60 Hz and DP-1 2560x1440 @ 75 Hz, i.e. two
  vblank grids. The policy predicts a single compositor grid. Run experiments on one display
  (disable DP-1) unless testing multi-monitor on purpose, and record the setup in `meta.txt`.
- Hyprland's own animations are compositor (HIGH) work, so they are not NORMAL bursts; client-side
  animations (GTK, Firefox, burst_client) are the NORMAL-burst case.
