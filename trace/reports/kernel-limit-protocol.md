# Gfx admission experiment and decision protocol

2026-11-04. Prospective design update. No kernel patch, soak or new capture is
claimed. The numerical registration below is a proposal to freeze before the
confirmatory matrix; it is not an already registered experiment.

## Priority and independent tracks

Prepare the fair-starvation report first. Kernel implementation and real-app
presentation instrumentation can then progress independently; neither must wait
for a fitted simulator. Reuse m3's synthetic burst workload and presentation log
for the first kernel go/no-go. GTK/Firefox need additional protocol/compositor
timing before equivalent presentation claims are possible.

On 2026-11-04, the official GitLab branch API and `git ls-remote` resolve
drm-misc-next to `e32e83a6c5ec031a7f59d4794113d8484b220254`.
Pinned files were read through the official repository API:

- `sched_rq.c`: SHA-256
  `634142eb99ad9a0110cb1c2fcd5b3944a3548dd05422f19730c30939242480d6`,
  identical to the reviewed v7.2.11 file. In `drm_sched_rq_pop_entity()`, the
  runtime-to-vruntime call remains inside `if (next_job)`; the empty branch
  removes the entity. The implicated path has not been replaced in this snapshot.
- `sched_entity.c`: SHA-256
  `e6183847bb4bb2598fe70934113d2e9a25ec9d1c2e54620f792b0e695ad4ba2a`.
  The locked pop still calls `drm_sched_rq_pop_entity()` after queue removal.

Sources: [branch API](https://gitlab.freedesktop.org/api/v4/projects/drm%2Fmisc%2Fkernel/repository/branches/drm-misc-next),
[pinned rq source](https://gitlab.freedesktop.org/drm/misc/kernel/-/blob/e32e83a6c5ec031a7f59d4794113d8484b220254/drivers/gpu/drm/scheduler/sched_rq.c),
[pinned entity source](https://gitlab.freedesktop.org/drm/misc/kernel/-/blob/e32e83a6c5ec031a7f59d4794113d8484b220254/drivers/gpu/drm/scheduler/sched_entity.c).
Recheck before sending a report. This verifies source persistence, not a fresh
reproduction on drm-misc-next or every internal branch in the original traces.

## Exactly what is capped

| Mode | Extra admission rule on each measured gfx scheduler | HIGH/KERNEL |
|---|---|---|
| `off` | Original capacity and ordering | Original behavior |
| `bg1-gfx` (NORMAL/LOW cap; selection is a separate factor) | At most one combined NORMAL/LOW scheduler-dispatched, unfinished job | Exempt from this extra cap; still consume original total credits |
| `global1-gfx` (separate diagnostic) | Total scheduler `credit_limit=1` | Included in the one-job total |

An unfinished job has been dispatched by drm_sched and has not completed through
its normal completion/error path, including time waiting on the hardware ring.
Software-queued/dependency-blocked work does not count. Neither rule preempts a
running job or bounds its duration. This is per scheduler, not device-wide; record
all gfx rings used. SDMA, compute and video admission remain unchanged. Disable
user queues and audit direct submissions, which are outside the scheduler cap.

`bg1-gfx` applies to **all** NORMAL/LOW clients combined, including interactive
victims. It is not a background-hog classifier and can remove a victim's own
pipelining. It cannot be implemented by setting total `credit_limit=1`.

Admission and selection are separate factors. FIFO checks separate priority run
queues in priority order, so HIGH/KERNEL selection precedes a cap-blocked NORMAL
queue. Fair uses a single tree and stock selection stops with `-ENOSPC` at the
first ready entity that cannot fit. Merely adding the cap can therefore leave a
HIGH entity behind a blocked NORMAL entity. Nominal priority exemption alone does
not ensure HIGH/KERNEL progress.

Define selection modes explicitly:

- `stock`: preserve selection's stop-on-insufficient-capacity rule, including
  rejection by the additional cap. Fair/bg1/stock is a head-of-line-blocking
  diagnostic, not the priority-exempt implementation's claimed successful result.
- `skip-cap`: continue scanning past an entity blocked **only by the additional
  NORMAL/LOW cap**, retaining tree order among eligible entities. Stop on original
  total-credit insufficiency; do not broaden the change to all `-ENOSPC` cases.
  This modifies fair's effective service ordering. It is a distinct intervention,
  not a fairness-preserving implementation detail or a sleeper-accounting fix.

The fair implementation requires more selection, locking and lifecycle work than
FIFO admission alone. Trace skip reasons/counts, nodes examined, selected priority,
selection cost and waiting time by client/priority; test a blocked NORMAL ahead
of a ready HIGH/KERNEL entity. Bound scan cost and check progress when every
candidate is blocked, without spinning or losing completion wakeups. Preserve the
original total credit bound. Track reservation release through completion,
cancellation, entity destruction and reset. Do not infer isolation or fairness
from the combined cap invariant alone.

## Minimum-two clamp: audit before implementing

The v7.2.11 parameter-validation code explicitly clamps
`amdgpu_sched_hw_submission` to at least two and rounds it to a power of two.
`amdgpu_ring_init()` uses this value for `ring->num_hw_submission` and hardware
ring allocation; `amdgpu_fence_driver_init_ring()` uses that field for fence-slot
allocation. Scheduler initialization separately copies the field into
`args.credit_limit`. Therefore the global parameter is not a scheduler-only knob.

The historical reason for the minimum and the complete set of direct/recovery
path assumptions are **not yet established** by this review. Inspect the introducing
commit and subsequent fence/ring/reset users before relying on one. Preserve the
existing physical ring/fence capacity for both experimental modes, changing only
gfx scheduler admission. Do not remove the driver-wide clamp. A scheduler-only
limit is not declared safe merely because physical allocation is unchanged.

Before measurement, verify admission invariants with mixed priorities and two
hogs, cancellation/error completion and timeout/reset recovery. Include a short
soak (30 minutes of mixed GPU use and three suspend/resume cycles), inspect logs
and confirm forward progress after each. Record the exact recovery exercise;
ordinary trouble-free use is not a GPU hang-recovery test. Stock kernel, modules
and default boot entry stay untouched.

## Proposed registration to freeze before confirmatory runs

All arms use the identical test-kernel binary, config and patch set. Arm names
record ordering policy, admission and selection as separate fields:

| Arm | Ordering | Admission | Selection | Comparison purpose |
|---|---|---|---|---|
| `fifo/off/stock` | FIFO | Original | Stock | FIFO control |
| `fifo/bg1/stock` | FIFO | NORMAL/LOW cap one | Stock priority selection | Cap effect under FIFO |
| `fair/off/stock` | Fair | Original | Stock | Fair control |
| `fair/bg1/stock` | Fair | NORMAL/LOW cap one | Stop at cap-blocked ready head | Cap-only diagnostic, including HIGH blocking |
| `fair/bg1/skip-cap` | Fair | NORMAL/LOW cap one | Skip only extra-cap-blocked entities | Cap plus explicitly modified selection |

Compare `fair/bg1/stock` against `fair/off/stock` for cap-only behavior, and
`fair/bg1/skip-cap` against `fair/bg1/stock` for the selection change with the cap
held fixed. The comparison against `fair/off/stock` measures their combined
intervention; never label that contrast simply “fair + cap.” With admission off,
the extra-cap-blocked predicate is always false; `skip-cap` should reduce to stock
selection. Verify that property before the matrix rather than claiming an
independent selection effect with no cap.

Use opt-in admission and selection selectors, default off/stock; switch only at
initialization or through an audited drained transition. Boot-only selectors are
sufficient. Do not compare different builds as the causal contrast. Any sleeper
repair becomes another pinned factor evaluated with admission/selection explicitly
held fixed; never relabel it as stock fair. Neither admission nor skipped-head
selection repairs empty-queue runtime accounting by itself.

Proposed fixed budget: **six independent blocks**, each containing the five arm
sessions (**30 boot/login sessions** if each arm needs a separate boot), three run
repetitions per workload cell. This replaces the earlier four-arm/24-session
proposal; it is not an unreported addition after observing outcomes. Publish a
counterbalanced arm order and randomized within-session workload order before
starting. No early success stopping or adding sessions after seeing significance.
A safety failure stops the experiment; an invalid session is logged and replaced
only under predefined provenance/trace validity rules. Keep pilots and soak runs
outside the confirmatory dataset.

Before each session verify eDP-1 as the only active output; record exact Hyprland
and Mesa builds, render options, geometry, refresh, policy, admission and selection modes, user
queue setting, boot ID/age, DPM/GFXOFF settings and source/config/module hashes.
Freeze those settings across arms. Preserve all outcomes and missing feedback.

Use m3 cells: idle, heavy Vulkan, `vk-hog1`, heavy off-screen GL and tiny-job flood.
Add a throughput-only tiny-job cell so presentation pacing does not set its rate.
Rerun the cooperating-hog reference with current builds/settings; historical m3
numbers are context, not paired controls. Retain burst_client timing unchanged and
add cap validation tracepoints alongside the existing harness where needed.

### Victim self-cost and two-client cells

Alongside unchanged m3 controls, add explicit victim cells; these extensions are
not already implemented by reusing the old harness:

- One NORMAL interactive victim, no hog: both the original single-job-per-frame
  control and a multi-job-per-frame variant. Hold total GPU work/frame constant;
  use at least three individually timestamped jobs/frame with documented
  dependencies, and verify the actual scheduler-job count in traces. Avoid a CPU
  wait after each submission that would eliminate the original pipelining.
- Two independent NORMAL interactive victims, no hog: run matched single-job and
  multi-job workloads both aligned and offset by half a refresh. Record each
  client's IDs, frame/job mapping, timestamps, delivered/discarded frames and
  individual latency; do not pool their outcomes. Compare cap on/off with the
  **same two-client workload**, and retain each client's solo reference.
- After presentation instrumentation is validated, repeat with a real multi-job
  client (e.g. Firefox), verifying jobs/frame rather than assuming the browser's
  submission pattern. Synthetic multi-job coverage does not establish browser
  presentation behavior.

For each victim/frame report summed GPU execution time separately from the span
from first job submission to last job completion, inter-job dispatch gaps,
action/intended-frame to actual presentation, and missed refreshes. Constant GPU
execution with increased between-job gaps is still a victim regression. Record
first-frame and steady outcomes; keep multi-job instrumentation separate from the
unchanged m3 historical control.

### Proposed decision thresholds

Apply the cost gates per victim, workload and arm contrast; report FIFO and each
fair contrast separately. A combined fair-cap/selection gain cannot be attributed
to admission alone.

- Mechanism replication: heavy-hog GPU-start median with kernel cap is within
  1 ms of the same-block `vk-hog1` reference, and traced NORMAL/LOW occupancy
  obeys the cap. This tolerance is a proposed engineering criterion.
- Heavy-hog throughput loss at most **5%** relative to the uncapped stock arm.
- Tiny-job-flood throughput loss at most **5%**.
- Under heavy-hog contention, frame-0 presentation p50 improves by at least
  **one measured refresh** (~16.67 ms at 60 Hz), with a paired 95% confidence
  interval excluding zero. This is a target, not a prediction for every m3 cell.
- **Idle victim self-cost:** for both single-job and multi-job cells, summed GPU
  execution/frame and first-submit-to-last-completion p50 increase by at most
  **5%**; first-frame and steady presentation latency p50 increase by at most
  **1 ms**; missed-refresh rate increases by at most **one percentage point**.
  Evaluate each metric separately against the same-policy uncapped stock control.
- **Two-client self-cost:** apply those same execution/span/latency/miss-rate
  limits to **each** interactive client relative to the uncapped two-client cell,
  for aligned and offset patterns. Each client's delivered frame rate may fall
  by at most **5%**. Report maximum wait and consecutive missed-refresh streaks
  individually; an improved average across clients cannot hide one losing service.
- No credit leaks, persistent stalls or failed recovery/suspend checks.

All numerical limits, including the new victim gates, are proposed choices to
freeze before confirmatory runs, not user-approved tolerances or measured results.
For upper-bound cost gates, require the paired 95% interval's upper endpoint to
stay within the bound; an interval crossing it is inconclusive. A heavy-hog gain
does not pass the overall gate if idle or two-client self-cost fails. Use the
cap-only and selection-only-under-cap fair contrasts to explain any regression.

Summarize outcomes per run/session; estimate paired differences by resampling
complete independent blocks, never frames independently. Report effect sizes and
95% intervals for throughput costs as well as latency. An interval crossing a
decision boundary is inconclusive, not a pass. Six blocks may yield imprecise
intervals; report that result without extending the fixed confirmatory sample.
Any follow-up uses a new registration. Do not infer reliable p99 from a few dozen
first frames.

Keep `global1-gfx` distinct. Register its session count/order before its diagnostic
captures; do not mix them into the primary cap comparison or attribute priority
exemption benefits to adaptive classification. If a fixed limit works, retain it.
If it hurts throughput, use those measured costs to motivate adaptive admission.
If GPU waits improve without presentation, investigate pacing before adding
prediction/holds. Simulator fitting and `sched_policy=3` remain conditional;
investigate integration into fair for the upstream path.
