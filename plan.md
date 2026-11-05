# plan.md — real traces, real hardware

Read this first: where the project stands, what is decided, and what to do next.
Update **Where we are**, **Key findings** and **Next steps** whenever a step finishes.
Run-by-run detail and the dated status log up to 2026-11-04 are in
[the measurement log](trace/reports/measurement-log.md); topic reports are linked below.

## Where we are (2026-11-04)

1. **Simulator phase: done and frozen.** `gpusim/` + `policy.py` reached dev
   `jank_score` 16.26 (upstream-fair approximation 59.9, FIFO 88.0) after 60 agent
   experiments. Do not tune `policy.py` or score the holdout. It stays frozen until
   hardware results call for simulator fitting (phase 2, conditional).
2. **Measurement study (phase 1): mostly done.** Matrices m1–m3 attribute burst
   delay behind a heavy hog to its two in-flight jobs. Real-app presentation
   timing and the write-up (1.6) are still open.
3. **Kernel experiment: the test kernel is built, installed beside stock and booted.**
   The first boots showed that an in-kernel cap on background jobs **combined with
   the fair policy** reproduces the cooperating-hog benefit without changing the hog.
   Safety checks (soak, 3 suspend/resumes, forced GPU reset) passed for that arm.
   Next: the missing pilot test cells, then a 6-boot pilot.
4. **Upstream fair-starvation bug report: drafted, not sent.**

## Key findings so far

1. **The wait behind a heavy hog is in-flight depth, not the scheduler's pick.**
   With `credit_limit` 2, a burst frame waits for the hog's two non-preemptible jobs
   (~4 ms leftover + one 8 ms job). The pick itself costs ≤ 0.2 ms, and DPM barely
   matters (m1–m3).
2. **One hog job in flight halves it.** A hog that keeps one job in flight
   (`vk-hog1`) cuts GPU start latency from ~14–15 to ~5–6 ms p50 (p99 ~17 → ~9.5 ms),
   costing the hog 0–5% fps. Frame 0 is presented ~10 ms earlier (m2–m3).
3. **Ordering alone does not fix it.** FIFO, fair and RR give similar waits behind
   a heavy hog (m3). Fair helps frame 0 a little, but **starves after-idle frames
   behind a tiny-job flood**: 21/60 frame-0s waited ≥ 5 ms, up to 15.5 ms, vs 0/100
   under FIFO/RR. Cause: fair skips runtime accounting on empty-queue pops
   ([fair-sleepers report](trace/reports/fair-sleepers.md)).
4. **Neither cap nor ordering alone; both together work** (first test-kernel
   boots, [report](trace/reports/kernel-cap-first-boots.md)). Kernel-side wait until
   GPU start:

   | Condition | Victim p50 | Hyprland (HIGH) p50 |
   |---|---|---|
   | m3, uncapped heavy hog (FIFO or fair) | 12–15 ms | 10–13 ms |
   | m3, cooperating `vk-hog1` | 6.2–6.5 ms | 3.6–3.9 ms |
   | FIFO + cap | 12.7 ms | 3.7 ms |
   | **fair + cap (skip-cap)**, 3 runs | **5.3–6.2 ms** | **3.4–3.8 ms** |

   Under FIFO the cap protects only the compositor: the hog's next job is already
   queued and older, so FIFO serves it before the victim (697/700 victim jobs).
   Fair serves the least-serviced client, so the victim goes next (897/900 victim
   jobs had no hog job ahead). One boot per arm: pilot evidence, not a result.
5. **The fair + cap arm survived the safety checks:** ~80 min of use, 3/3
   suspend/resumes, and a forced MODE2 GPU reset with a job in flight (cancelled,
   slot released, 3,689/3,689 acquire/release matched, later work dispatched).
   Hyprland itself crashes on any GPU reset (userspace, not the cap).
6. **Other effects measured:**
   - GFXOFF exit adds ~1.0 ms before the first job after ≥ 100 ms idle, plus
     ~0.4 ms slower exec. DPM's steady effect is small on eDP-1.
   - Compositor pacing sets steady presentation (~34 or ~50 ms, varying by
     session), independent of GPU scheduling. GPU scheduling shows in frame 0 and
     the tails ([pacing report](trace/reports/compositor-pacing.md)).

**Novelty position** ([review](trace/reports/novelty-baselines.md)): limiting
in-flight work is prior art (V3D, TimeGraph), and upstream already discussed the
fair re-entry problem. The candidate contribution is a measured, transparent
kernel intervention for ordinary graphics clients, plus evidence that the cap
needs service-based ordering. It is not a new general mechanism.

## Rules and decisions

- **Daily laptop.** Kernel changes only go into the side-by-side `linux-drmsched`
  kernel. Never modify the stock kernel, its modules (`/lib/modules/7.2.11-arch1-1`)
  or the default boot entry.
- **The agent cannot sudo.** Give the user root commands with **absolute paths**
  (their terminal is often elsewhere). Never run `score.py --split holdout`.
- **Primary claim:** interactive bursts (launches, animations, first frames after
  input) suffer GPU delay **caused by GPU scheduling**, not CPU, DPM or compositor.
  Games and mpv are comparison workloads. Hyprland only. Free, scriptable hogs only.
  The synthetic `burst_client` stays the ground-truth control.
- **Experiment discipline** ([protocol](trace/reports/kernel-limit-protocol.md)):
  - One binary for all arms; arms differ only by boot command line
    ([arms.csv](trace/kernel/arms.csv) is the single registry).
  - Pilots never enter confirmatory data, and the block count is chosen from pilot
    variance before confirmation.
  - **Primary contrast: fair/off vs fair/bg1/skip-cap.** FIFO off/bg1 is a
    secondary compositor-protection contrast; fair/bg1/stock and global1 are
    diagnostics.

## Machine and boot setup

- Ryzen AI 7 350 / Radeon 860M (GC 11.5.2), Hyprland 0.56.2 (Lua config), Mesa 26.2.5,
  stock kernel **7.2.11-arch1-1**, systemd-boot + UKI, Secure Boot off.
- `gpu_sched.sched_policy`: 0 RR, 1 FIFO (stock default), 2 fair (experimental).
  Hyprland renders at HIGH on `gfx_0.0.0`, the same ring as clients. No client uses
  user queues.
- Displays: eDP-1 1920x1200@60. A DP-1 dock (2560x1440@75) has been attached at
  times; **verify eDP-1 alone before captures.**
- **Test kernel `7.2.11-arch1-1-drmsched`** (recipe and patches in `trace/kernel/`):
  - Installed as package `linux-drmsched`. Six one-shot UKIs `drmsched-<arm>.efi`,
    built by `/etc/mkinitcpio.d/linux-drmsched.preset` from
    `/etc/kernel/cmdline-drmsched-<arm>` (sources: `trace/kernel/boot/`).
  - `default arch-linux.efi` in `/boot/loader/loader.conf` keeps stock as default.
    Stock boot hashes are in `.cache/stock-boot.sha256`.
  - Boot an arm for one boot only:
    `systemctl reboot --boot-loader-entry=drmsched-<arm>.efi`.
  - Verify the arm with `sudo dmesg | grep "drmsched experiment"`.
  - Undo everything: `sudo pacman -R linux-drmsched`.
  - **After any GPU reset, reboot before measuring** (the GPU timestamp counter resets).
- tracefs/debugfs need root. Captures go to `traces/` (gitignored).

## Next steps

### Kernel track (main line)

1. **Add the missing pilot test cells.** Agent work, no root; see the protocol's
   self-cost section.
   - **Solo multi-job victim:** ≥ 3 timestamped jobs per frame at constant total
     GPU work, with the jobs/frame count verified in traces.
   - **Two NORMAL victims**, aligned and offset by half a refresh.
   - **Throughput-only tiny-job flood**, not limited by presentation.
   - **Hog throughput (fps/jobs per second) in every summary.**
   - Add these cells to `trace/pilot.py plan` (it currently runs only m3 cells).
2. **Decide the primary GPU-benefit measure (the user's call).** The recommendation
   is kernel-side wait until GPU start (`trace/start_wait.py`), because the victim's
   own GPU-timestamp correction can be loose under load (it was ±2.4 ms in one run).
3. **Fair pilot: 3 boot pairs, 6 boots (the user runs it, as root).**
   - Generate the order with
     `python3 /home/jinho/Projects/drm-gpu-scheduling/trace/pilot.py plan --seed N --out PATH`
     (fair pair by default).
   - Boot each arm one-shot and run its session commands.
   - Check:
     - the mechanism against the same-boot-pair cap-off `vk-hog1`
     - trace loss under the flood
     - between-boot variance
     - hog throughput cost and victim self-cost
     - **whether the cap changes fair's tiny-job-flood starvation (finding 3)**
4. **Freeze the confirmatory design.** Use `trace/paired.py` for precision planning
   and fix the block count, order and endpoints. Then run the confirmatory blocks and
   write the result. If the fixed cap explains the benefit, keep the simple design.
5. **Secondary and diagnostic arms, later.**
   - FIFO + cap with matched burst settings (compositor protection).
   - fair/bg1/stock (head-of-line diagnostic).
   - `fifo/global1/stock`.
   - A real timeout-detected hang, and "a day of normal use" on the primary arm.

### Other tracks

6. **Send the fair-starvation report**
   ([handoff](trace/reports/fair-starvation-handoff.md)). Before sending:
   - Add **Tvrtko Ursulin**, author of the withdrawn min_vruntime fix, to Cc.
     `get_maintainer.pl --nogit` lists only maintainers, not patch authors.
     Consider adding Pierre-Eric Pelloux-Prayer (AMD).
   - Send as plain text with the compact evidence, from Gmail or `git send-email`,
     and offer the raw traces on request. The repo is public, so publishing the
     73 MB bundle is the user's separate decision.
   - Recheck drm-misc-next first.
   - Record the Message-ID.
7. **Real-app presentation track (independent).** Pilot GTK launches, Firefox
   scrolling, Hyprland animations and mpv per the
   [real-study protocol](trace/reports/real-study-protocol.md).
   - Add commit/latch/presented-buffer timing; map polling is not presentation
     timing.
   - Resolve the 34-vs-50 ms pacing modes with matched settings.
   - Needed for real-app presentation claims, not for the kernel go/no-go.
8. **Optional: FIFO right after a fresh boot, eDP-1 only**, to settle the boot-age
   and second-display confound in m3-p1r. The command is in the measurement log
   (former Next 5).
9. **Conditional, only if hardware results call for it:**
   - Simulator fitting (phase 2).
   - A smaller policy port (3.2), preferably as changes to fair rather than a new
     `sched_policy=3`.

## Research direction and publication gates

**Target:** a conference contribution on automatic control of in-flight background
GPU work for interactive responsiveness. A workshop or short measurement paper is
the nearer milestone. The first-boot result (fair + cap) points to a simpler
contribution than the full simulator policy. If the simple intervention explains
the gains, publish that narrower result rather than claiming the full policy.

**Full-paper gates:**
- [ ] Unmodified real victims (launches, scrolling, animations) under heavy GL/Vulkan
  loads, tiny-job floods and video controls.
- [ ] First-frame presentation latency and missed refreshes as primary outcomes,
  alongside GPU start latency, hog throughput, fairness, starvation and overhead.
  Separate GPU gains from compositor pacing.
- [ ] Ablations: in-flight cap, ordering/selection, sleeper credit/aging,
  classification, compositor prediction. Keep only what earns its place.
- [ ] Independent boot/session blocks, counterbalanced order, controlled session
  state, paired confidence intervals. No p99 claims from a few dozen first frames.
  Hold out evaluation workloads.
- [ ] A second hardware configuration if available; otherwise state the
  single-machine limit.
- [ ] Release scripts, pinned sources/patches, settings, analysis and shareable traces.

## Phase checklist

Details for phases 0–1 are in [the measurement log](trace/reports/measurement-log.md).

**Phase 0 — go/no-go (stock kernel): done, GO.**
- [x] 0.0–0.4: tools; tracepoints; all GL/Vulkan through drm_sched; Hyprland is HIGH
  and the only source of fence deadlines; DPM pinning and `gpu_metrics` sampling.

**Phase 1 — attribution study**
- [x] 1.1 Capture harness. [x] 1.2b per-frame join (validated). [x] 1.3a `burst_client`.
  [x] 1.4 hogs (`vk-hog`, `vk-hog1`, `gl-hog`, `vkmark-cube` flood, `stk`).
- [~] 1.2 Per-job breakdown done; a few details remain open.
- [~] 1.3b Real burst sources: tooling added (`workloads/real.py`); pilots,
  presentation timing and attribution pending.
- [~] 1.5 Matrix: m1, m2, m3 (policies 1/2/0) done. Open: fresh-boot FIFO repeat.
- [ ] 1.6 Measurement report with independent-run uncertainty and confounds.

**Phase 2 — simulator fitting: conditional.** Only if hardware results need adaptive
policy modelling.
- [ ] Tag the simulator as v1, measure its constants from traces, and fit client
  models (not replays).
- [ ] Model GFXOFF/DPM and fair's real accounting path, validate against hardware,
  and re-score.

**Phase 3 — side-by-side test kernel**
- [x] 3.1 `linux-drmsched` package built (Rust 1.99.1 config pin), installed beside
  stock, with six one-shot boot entries.
- [x] 3.1a Audit of the minimum-two clamp and its ring/fence dependencies
  ([audit](trace/reports/amdgpu-submission-audit.md)).
- [~] 3.1b Gfx admission/selection patch
  ([implementation](trace/reports/gfx-admission-implementation.md)).
  - KUnit and first hardware checks pass, with cap invariants verified on hardware.
  - Self-cost and throughput are not measured yet.
- [ ] 3.2 Conditional policy port. [ ] 3.3 Conditional sim/C equivalence test.
- [x] 3.4 `amdgpu.user_queue=0` default (separate patch) and admission/dispatch/
  selection tracepoints verified on hardware.
- [~] 3.5 Soak: fair/bg1/skip-cap done (80 min, 3 suspends, forced reset). Open: a
  real hang, a day of use, the other arms.

**Phase 4 — hardware A/B**
- [ ] 4.1 Pilot, then confirmatory blocks per the protocol (next steps 3–4).
  Real-app victims once presentation timing exists.
- [ ] 4.1b Mechanism ablations at matched throughput cost.
- [ ] 4.1c Second hardware configuration if available.
- [ ] 4.2 Success targets:
  - lower burst sched + ring wait and real first-frame presentation latency
  - the hog keeps ≥ 90% of its frame rate
  - two hogs stay fair (Jain ≥ 0.95)
  - a mimic gets ≤ 55% of the contended GPU
  - no job waits > 250 ms (NORMAL) / 1 s (LOW)
- [ ] 4.3 Final report, artifact release and submission decision against the gates.

## Tooling (`trace/`; captures in `traces/`, gitignored)

| file | what it does |
|---|---|
| `capture.sh` | root: `sudo trace/capture.sh NAME [SECONDS] -- WORKLOAD...` records trace-cmd (`-C mono`), bpftrace (`bpf/ids.bt`), fdinfo (`clients.py`), `gpu_metrics.py` and kernel/module/selector provenance while running WORKLOAD as the user in the Hyprland session; writes `traces/NAME/<timestamp>/` |
| `matrix.sh` | root: `sudo trace/matrix.sh [NAME]`: traced `burst.sh` runs per hog × DPM condition × `REPS` plus calibration, shuffled; `DRY=1` prints the order |
| `workloads/burst.sh` | `burst.sh HOG [BURST_CLIENT_ARGS]`: empty workspace, timestamp probe, hog warm-up, then burst_client. m3 settings: `--iters 736 -n 10 -i 300 -b 20` |
| `workloads/hog.sh` | background loads: `vk-hog` (8 ms jobs), `vk-hog1` (1 in flight), `gl-hog` (off-screen 8.5 ms), `vkmark-cube` (tiny-job flood), `glmark2-*`, `stk`, `mpv`, `none` |
| `burst_client/` | ground-truth Vulkan + Wayland burst source with per-frame CSV (submit, GPU start/end, presented); `burst_client --help` |
| `parse.py` | `python3 trace/parse.py RUN`: per-job breakdown to `RUN/jobs.csv` (`credit_wait` assumes credit limit 2 and does not know the bg1 cap) |
| `join.py` | `python3 trace/join.py RUN...`: burst.csv × trace → `RUN/frames.csv` with submit/dep/order/pick/credit/ring/wake split |
| `start_wait.py` | `python3 trace/start_wait.py RUN... [--ahead]`: victim/Hyprland kernel wait until GPU start (no GPU timestamp correction); `--ahead` counts hog jobs dispatched ahead of each ready victim job |
| `admission.py` | `python3 trace/admission.py RUN...`: validates bg1 cap invariants (occupancy, serials, priority exemption, trace loss) and reports selection cost |
| `burst_stats.py`, `compare.py` | per-frame-index latencies of one run; cross-matrix comparison after `join.py` |
| `pilot.py`, `paired.py` | `pilot.py plan --seed N --out PATH [--pair fair\|fifo]` prepares a 6-boot pilot (never executes); `pilot.py pacing`; `paired.py` paired-block t intervals, sign-flip check and precision planning |
| `pacing.py`, `fair_sleepers.py` | compositor pacing vs vblanks; fair empty-pop starvation check |
| `kernel/` | `linux-drmsched` recipe, `0001-gfx-admission.patch`, `0002-test-disable-user-queues.patch`, source manifests, `arms.csv`/`arms.py`, `boot/` presets; see `kernel/README.md` |
| `workloads/real.py`, `video.sh` | real-app victim runner (GTK launch, Firefox scroll, compositor, mpv) and mpv tooling |
| `gonogo.py`, `clients.py`, `gpu_metrics.py`, `dpm.sh`, `share_fair.py` | phase-0 checks, fdinfo sampler, SMU metrics, DPM level, fair-report bundle builder |

## Open risks

- **Self-cost of the cap is unmeasured.** Apps that submit several jobs per frame,
  or two interactive apps, now share one slot.
- **Fair's tiny-job-flood starvation** (finding 3) may persist or change under
  the cap.
- **Victim GPU-timestamp correction** can be loose under load. Prefer kernel-side
  measures, or re-probe on an idle GPU.
- **Session confounds:** boot age and a second display (DP-1) changed Hyprland's
  cost in m3-p1r, and pacing mode varies by session. Use eDP-1 only, record pacing
  probes, and counterbalance boot order.
- **Hyprland crashes on GPU reset** and restarts in safe mode (config untouched).
- **Single machine and compositor.** Generalization needs a second configuration.
