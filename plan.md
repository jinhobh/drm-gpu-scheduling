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

**Phase 0, step 0.1:** check that the tracepoints exist and record a first trace.
Then work down the checklist below in order.

---

## Phase 0: setup and go/no-go checks (stock kernel)

- [x] 0.0 Install the tools (done by the user).
- [ ] 0.1 List the available events and record their exact 7.2 names in this file:
  `sudo trace-cmd list -e | grep -E 'gpu_scheduler|amdgpu|dma_fence|drm'`.
  Expected (verify): `gpu_scheduler:drm_sched_job_queue/_run/_done/_add_dep/_unschedulable`,
  `amdgpu:amdgpu_cs_ioctl`, `amdgpu:amdgpu_sched_run_job`, `amdgpu:amdgpu_bo_move`,
  `dma_fence:*`, `drm:drm_vblank_event`.
- [ ] 0.2 **Go/no-go: does gfx work go through drm_sched?** Trace ~5 s while Hyprland animates,
  mpv plays, and vkmark runs. Every one of them must show up in `drm_sched_job_queue/run`
  (map entities to processes with `amdgpu_cs_ioctl` and the pid/comm fields). If a client is
  missing, it is on user queues. Record which clients use which path. On the stock kernel this
  is a finding in itself; the test kernel (phase 3) will set `amdgpu.user_queue=0`.
- [ ] 0.3 Check whether Hyprland's context is HIGH priority (ctx priority in the trace or
  fdinfo), and whether its fences, and client fences, carry deadlines (`dma_fence_set_deadline`
  via a bpftrace kprobe if there is no tracepoint). The policy reads the compositor and vblank
  grid from these. If either is missing, note what fallback the policy needs (e.g. identify the
  DRM master directly).
- [ ] 0.4 Check that `pp_dpm_sclk` / `power_dpm_force_performance_level` can be pinned to
  `high` and restored to `auto` (root), and that `gpu_metrics` can be sampled at ≥ 1 kHz.

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
- Hyprland's own animations are compositor (HIGH) work, so they are not NORMAL bursts; client-side
  animations (GTK, Firefox, burst_client) are the NORMAL-burst case.
