# Gfx admission experiment and decision protocol

2026-11-04. Prospective design with a subsequent
[prototype implementation](gfx-admission-implementation.md). A source patch and
mock/compile checks now exist; no production package, soak or new hardware capture
is claimed. The numerical registration below is a proposal to freeze before the
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

The prototype uses each job's immutable armed priority (`s_priority`) for
eligibility and reservation class, copying it at dispatch. Entity priority
changes do not retag already armed jobs. Record this convention when interpreting
priority-change tests and dispatch traces; use the explicit dispatch priority,
not an assumption that every queued job has its entity's latest priority.

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

## Minimum-two clamp: source audit completed, runtime checks pending

The [2026-11-04 source/history audit](amdgpu-submission-audit.md) identifies the
introducing commit, verifies the pinned downstream Arch sources and maps the
allocation, direct-submission and recovery dependencies. The
[separately named package recipe](../kernel/README.md) now includes the prototype
patch and source checks. Focused mock/compile checks pass; a full production
kernel/package build and real-device runtime validation remain pending.

The v7.2.11 parameter-validation code explicitly clamps
`amdgpu_sched_hw_submission` to at least two and rounds it to a power of two.
`amdgpu_ring_init()` uses this value for `ring->num_hw_submission` and hardware
ring allocation; `amdgpu_fence_driver_init_ring()` uses that field for fence-slot
allocation. Scheduler initialization separately copies the field into
`args.credit_limit`. Therefore the global parameter is not a scheduler-only knob.

Introducing commit `5d5bd5e32e6e` specifies minimum two but does not explain its
hardware rationale. The static audit identifies direct/recovery paths that the
patch must handle; their progress and safety under the new cap still require
implementation review and runtime validation. Preserve the
existing physical ring/fence capacity for both experimental modes, changing only
gfx scheduler admission. Do not remove the driver-wide clamp. A scheduler-only
limit is not declared safe merely because physical allocation is unchanged.

Before measurement, verify admission invariants with mixed priorities and two
hogs, cancellation/error completion and timeout/reset recovery. Include a short
soak (30 minutes of mixed GPU use and three suspend/resume cycles), inspect logs
and confirm forward progress after each. Record the exact recovery exercise;
ordinary trouble-free use is not a GPU hang-recovery test. Stock kernel, modules
and default boot entry stay untouched.

## Pilot first; registration is not frozen

The earlier six-block/30-session proposal and the many per-cell CI gates are
withdrawn. Choose the confirmatory block count from between-boot pilot variation,
then fix it **before** any confirmatory data. Pilot/soak runs never enter that data.
**Revised 2026-11-04 after the first boots, before any pilot or confirmatory
data** ([first-boot report](kernel-cap-first-boots.md)): the primary contrast is
**fair/off/stock versus fair/bg1/skip-cap**. Under FIFO the cap cannot give a
NORMAL victim the GPU-start benefit: the hog's next job is already queued and
older, and FIFO serves it first (697/700 victim jobs). FIFO cap-off versus bg1
remains a secondary contrast for HIGH compositor protection; fair/bg1/stock
(cap only) and global1 remain diagnostics. Do not multiply three primary claims
across all arms, clients, cells and metrics.

### One arm registry

[arms.csv](../kernel/arms.csv) is the sole normative arm/boot-selector
registry, including contrast controls and primary/secondary/diagnostic roles.
`python3 trace/kernel/arms.py ARM` emits explicit boot arguments, including user
queues off. Other documents link here rather than maintain copied arm tables.
All comparisons use one identical test-kernel binary, config and patch stack.
Sleeper repairs are separate pinned factors, never relabeled as stock fair.

For the mechanism reference, compare a capped `vk-hog` cell with **cap-off,
stock-selection `vk-hog1`, under the same ordering policy, in the paired block's
uncapped boot**. For FIFO this is the registry's FIFO control; for fair it is its
fair control. The capped arm's own `vk-hog1` is an additional self-cost observation,
not the mechanism reference. Historical m3 runs are not paired controls.

### Fair feasibility and variance pilot

Start with **three independent fair off/bg1-skip-cap boot pairs (six boots;
`pilot.py plan --pair fair`, the default)**, not six
confirmatory blocks. Counterbalance which arm boots first and record a seeded
order before running. `trace/pilot.py plan --seed N --out PATH` prepares commands
without executing them. It uses the existing m3 cells: idle, heavy Vulkan,
cap-off `vk-hog1` reference and tiny-job flood, three run repetitions, DPM auto.
It adds short no-hog pacing probes before/after each session matrix. Keep one
active eDP-1 output, fixed geometry/userspace/options, no suspends during a
session, and record boot age, all selectors, user-queue setting, source/config/
module hashes and DPM/GFXOFF. The separately named kernel/recovery checks remain
prerequisites; no command here installs or boots a kernel automatically.

The pilot must establish these before registration:

1. **Mechanism:** capped heavy-hog GPU-start latency approaches cap-off,
   same-policy `vk-hog1` (proposed engineering tolerance: absolute difference of
   run-median averages <= 1 ms); inspect traced credit/ring/pick decomposition and
   reservation invariants. Report pilot intervals; this tolerance is a feasibility
   check, not a claim of equivalence proven by a small sample.
2. **Trace capacity:** stress the tiny-job flood, record event rates/buffer
   overruns/lost records and selection cost. The patched selection event emits
   only when `examined > 0`, suppressing empty FIFO queues. This reduces traffic;
   it does not establish loss-free capture. Tune buffers/event configuration in
   pilots and pin it before confirmation. Preserve every loss/failure as a pilot
   outcome. Confirmatory loss invalidates that run's trace attribution, not an
   entire boot or its still-valid timing/throughput observations by default.
3. **Between-boot variability:** estimate paired differences for all primary
   scalar outcomes below. The initial m3-only pilot cannot size the self-cost
   endpoint: first implement the multi-job/two-victim cells, then run complete
   additional pilot blocks containing them. Do not freeze a count from GPU-start
   variance alone or label missing self-cost data as a pass.

Preserve pilot IDs and all outcomes, including failed/missing runs. Record
replacement criteria before confirmation (provenance/output/trace loss and missing
feedback by endpoint), not after seeing an arm's latency. A safety failure stops
work; it is not a conveniently replaceable invalid session. Freeze a balanced
session order, randomized within-session workload order, full endpoint/missing-data
rules, sample count and resource bound before confirmation. No success stopping
or extending the count after seeing its significance.

### Pacing covariate and presentation validity

Record each pre/post no-hog probe with `pacing.py --csv`; classify paced warm
callback-to-presentation delays as 2-refresh, 3-refresh, mixed or unknown.
`pilot.py pacing FILE --refresh-ms T` implements a proposed descriptive classifier:
at least 10 valid paced warm samples, with >=80% within 0.25 refresh of two or
three; otherwise mixed/unknown. Preserve the proportions and missing feedback,
not just the category. Thresholds must be checked in pilots and frozen.

A stable 3-refresh session is **not invalid** just because another boot is stable
2-refresh. A pre/post mode change is also an outcome, not an automatic whole-boot
exclusion. Record both categories as descriptive covariates; show unadjusted
presentation contrasts and matched-mode sensitivity analyses. The probe itself
runs under the assigned cap, so its mode can be treatment affected: conditioning
on it is not automatically a causal correction. Never discard a cap-induced mode
change or silently rebaseline it away. Missing/ambiguous output/timing invalidates
only the affected presentation attribution; retain independent GPU outcomes.

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

### Three statistical primary criteria and one safety prerequisite

These are **proposed** engineering targets for the primary fair off versus
bg1/skip-cap decision (originally written for FIFO; see the revision above), to
review using feasibility/precision pilots before freezing. The unit of inference
is the independent paired boot block. Average the prespecified run summaries
within each session/cell first. A good p50 across clients cannot hide one losing
service, but secondary per-cell CIs are not dozens of extra mandatory gates.

1. **GPU scheduling benefit:** block difference of first-frame GPU-start latency
   (uncapped heavy `vk-hog` minus capped heavy `vk-hog`, using averages of run
   medians), in ms. Require the paired two-sided 95% t interval's lower endpoint
   > 0. Report first-submit-to-last-completion span, summed exec and inter-job gaps
   alongside it to attribute the gain. Mechanism matching to the uncapped
   `vk-hog1` reference is the pilot prerequisite above.
2. **Interactive GPU self-cost:** use the prespecified two-client, three-job/frame,
   no-hog aligned and half-refresh-offset cells. In each block, take the maximum
   of the four per-client/cell log ratios of run-median GPU spans (capped/uncapped).
   Require the t interval's upper endpoint <= log(1.05). This conservative
   worst-client scalar is **one** primary endpoint, specified before data; report
   all four components. Other solo/single-job/first-vs-steady exec/span/gap results
   remain secondary. A material secondary regression warrants a stated limitation
   or new experiment, not an unreported extra pass/fail hypothesis.
3. **Throughput cost:** in each block, take the maximum log loss log(off/on) for
   heavy `vk-hog` FPS and a throughput-only tiny-job-flood rate. Require its t
   interval's upper endpoint <= -log(0.95). Report both component costs and their
   intervals. Verify the tiny-job cell is not presentation limited in pilots;
   that throughput-only extension remains to be implemented/validated.
4. **Safety/integrity prerequisite (not a CI test):** no credit leaks, persistent
   stalls, invariant violations or failed recovery/suspend checks. Trace validity
   is assessed for each affected endpoint/run. Failure cannot be outweighed by
   favorable latency or treated as a missing successful session.

The two per-block maxima are intentionally conservative; they estimate mean
worst-client/worst-load cost across blocks, not a guarantee for every frame or
future workload. Examine their pilot distributions before selecting an interval
model/count. If the proposed tolerances are not measurable or feasible, revise
and document the design **before** confirmation, rather than manufacturing a pass.
Fair contrasts and other cells use the same reported GPU metrics as secondary
analyses, with no joint all-arm pass claim. A later fair/presentation claim needs
its own primary question and registration.

### Presentation and other secondary outcomes

Retain real presentation measurement, but remove the 1 ms median limits and the
mandatory one-refresh p50 improvement gate. Report block differences in **mean
first-frame and steady presentation latency**, in ms and measured refresh units;
also report the share of frames that slip an additional refresh against a
prespecified target derived from the matched uncapped calibration/output grid.
Use the same target convention in both arms; do not calibrate away an arm's mode
change. Freeze exact target/feedback/denominator rules after the pilot validates
that instrumentation. Report discarded and missing feedback explicitly; do not
silently omit failures from the slip-rate denominator or replace their latency
with zero. Medians/distributions remain descriptive, not 1 ms pass/fail tests.

Report exec time, inter-job gaps, span, start delay and throughput for every
victim/cell; individual delivered FPS, maximum wait and missed-refresh streaks
for two-client cells; selection cost/fairness and HIGH waiting. These are
secondary effect estimates with uncertainty and multiplicity disclosed. The
kernel go/no-go now prioritizes GPU attribution. A paper claiming responsiveness
still needs a well-powered presentation result, independently registered.

### Small-sample inference and block-count choice

Use the paired block values d_b and **mean(d) +/- t_(.975,B-1) s(d)/sqrt(B)**,
not a percentile bootstrap over six observations. The paired t interval assumes
independent blocks and adequately modeled block differences; it is not a cure
for skew, outliers or pacing mixtures. Show every block and inspect the pilot
shape. An exact two-sided sign-flip test is a sensitivity check only when null
sign symmetry/exchangeability is defensible; with six pairs its smallest p is
2/64 = 0.03125. It does not yield model-free precision or rescue a bad design.
See [NIST's paired-difference interval](https://itl.nist.gov/div898/handbook/prc/section3/prc312.htm).

`trace/paired.py` accepts one scalar per stage/block/metric, rejects duplicate
blocks, mixed pilot/confirmation and different block sets, and reports t intervals
and sign-flip sensitivity. For pilot-only planning it searches a prespecified
candidate range for t_(.975,B-1) * inflation * pilot_SD / sqrt(B) <= target
half-width. Suggested planning inputs are 1.5 SD inflation and half-widths of
0.5 ms (GPU benefit), log(1.025) (self-cost), and -log(0.975) (throughput cost).
These are precision-planning choices, not power guarantees or frozen sample size.
Pilot variance from three blocks is uncertain; extend the feasibility pilot if
needed, and reject zero-variance sizing rather than infer infinite precision.
Choose a practical candidate/resource range before the planning call (tool
default 8..60); an infeasible result calls for redesign, not clipping to a passing
count. Select one common count meeting all primary precision targets, publish
that count/order/analysis specification, then start fresh confirmation. Report
inconclusive intervals without post-hoc extension. Do not bootstrap frames or
infer reliable p99 from a few dozen first frames.

Global1 remains a separate diagnostic with its own planned session budget. Keep
adaptive admission and simulator fitting conditional on measured costs/benefit.
If GPU waits improve without presentation, investigate pacing before adding
prediction/holds or claiming desktop responsiveness.
