# Gfx admission prototype: implementation and validation

2026-11-04. Implements the next kernel-code step, after the
[minimum-two audit](amdgpu-submission-audit.md). This is a tested source patch,
not a built production kernel package, hardware safety finding or latency result.

## Delivered behavior

[The patch](../kernel/0001-gfx-admission.patch) applies after Arch's pinned
`v7.2.11-arch1` patch. It introduces boot-only AMDGPU gfx admission and selection
parameters, independent scheduler reservation state and experimental tracepoints.
[The recipe and arm table](../kernel/README.md) specify exact controls. All five
primary arms and the separate all-priority diagnostic use one patch/config/build;
defaults are off/stock. Test-kernel user queues default off. Physical ring/fence
storage and the driver-wide minimum-two parameter stay unchanged.

Every existing in-tree `drm_sched_init_args` caller uses an aggregate initializer,
so added fields default to zero outside AMDGPU's explicit gfx opt-in. AMDGPU
resets those fields on every loop iteration, including non-gfx schedulers. The
patch changes internal struct layouts: the complete kernel/module set must be
rebuilt under the distinct test release. It is not a loadable replacement module
for the stock kernel.

## Reservation lifetime and selection

The cap counts combined NORMAL/LOW scheduler-dispatched unfinished jobs, rather
than original credit units. The job's armed priority (`s_priority`) is the stable
admission class; the same value is used by eligibility and dispatch. The source
copies it into fence-owned state at successful dispatch. Entity priority changes
or entity destruction cannot change the release class. Already armed jobs retain
their class when an entity is boosted.

Only the serialized `work_run_job` acquires reservations, after a successful pop
and before backend submission. Eligibility probes and failed/dependency-blocked
pops acquire none. A callback attached to the scheduler's **finished** fence owns
an extra fence reference and releases the reservation exactly once. Callback state
lives in `drm_sched_fence`, not the job or entity, so concurrent job cleanup does
not invalidate its storage. Transitions and their serial IDs are serialized by a
scheduler-local spinlock; the callback does not take run-queue locks.

Consequences for the audited lifecycle paths:

- Normal, immediate error, null-return and already-signaled backend completions
  finish the scheduler fence and release through the same callback.
- Generic stop/start detaches/reinstalls hardware callbacks and credits without
  finishing the job. Its reservation **stays held**; it is neither dropped nor
  charged again on restart. Restart cancellation releases on finished signaling.
- Direct exceptional finished-fence signaling also triggers the callback. A later
  hardware callback does not release a second reservation.
- Job cleanup removes any still-pending callback under the finished-fence lock,
  releases a canceled reservation and drops its extra reference. If completion
  already owns/runs the callback, removal synchronizes and cleanup does not
  release again. No counter is blindly zeroed during reset.
- Release queues submission work when the scheduler is running. While stopped,
  restart performs the wakeup through the existing workqueue mechanism.

These choices do not repair the pre-existing AMDGPU emergency helper described
as incorrect/racy in the audit. In particular, directly signaling a scheduler
fence is not proof of hardware quiescence. The direct-signal KUnit case stops
workers until the late hardware callback completes. Actual driver recovery must
still be validated on the test kernel.

Total-credit insufficiency is checked **before** the additional cap and has its
own reason. Stock selection stops at either failure. Skip-cap selection continues
only for the additional-cap failure, retains eligible tree order, and returns
without requeuing itself when every candidate is blocked. One scan visits at most
each tree node once: O(entity count), with no fixed numeric latency budget. Trace
both scan size and elapsed wall time (including lock acquisition) before judging
cost under a job flood. This is a separate service-order intervention, not an
accounting repair or a guarantee of HIGH isolation/fairness.

## Trace and build provenance

Three experimental events leave the existing stable events unchanged:

| Event | Purpose |
|---|---|
| `drm_sched_admission` | acquire=0, finished release=1, cleanup release=2; device/ring, finished fence, client, saved priority, serial, occupancy, original credits/limit and error |
| `drm_sched_admission_dispatch` | each dispatch's armed priority, finished fence and reservation serial (zero for exempt/off), extra cap and original credit state |
| `drm_sched_admission_select` | examined/skipped nodes, final reason/selected priority and scan time; reasons 0=selected, -2=no head/ready job, -11=extra cap, -28=original credits |

`trace/admission.py` checks observed bg1 reservation/dispatch identities and bounds,
serial gaps, duplicate/unmatched transitions, priority exemption and trace loss.
It reports capture-boundary reservations explicitly; a boundary is not a leak.
There must be background dispatch evidence, not just self-reported counters.
This is not a global1 or direct-hardware-occupancy validator. Scan summaries are
observations, not a bound on future selection time.

`capture.sh` records both boot selectors and installed module artifact hashes,
plus the running config hash and packaged build manifest when present. The recipe
checks the audited base **before** the research patch and a second manifest after
it, then rejects config/release drift. Its successful-build manifest records the
source base, patch/config/manifest hashes, toolchain versions and image hashes.

## Validation and limits

[Recorded evidence](gfx-admission-validation.txt) accompanies these results:

- Focused KUnit tests in an isolated UML kernel with lock checking enabled:
  **13/13 fair**, **10/10 applicable FIFO**, three fair-only cases skipped under
  FIFO. Coverage includes combined NORMAL/LOW admission, HIGH/KERNEL exemptions,
  off-mode pipelining, all-blocked wakeups, entity priority/lifetime changes,
  stop/start cancellation and rearming, direct finished signaling, cleanup,
  immediate backend outcomes, blocked-head stock/skip and original-credit stops.
- Compiled all scheduler objects and linked `gpu-sched.o`, plus AMDGPU's changed
  `amdgpu_device.o` and `amdgpu_drv.o`, for x86 with the Arch config after Kconfig
  dropped unavailable Rust. No compiler errors/warnings in those checks. This
  is compile-only coverage, not the accepted production config or module build.
- Broader existing scheduler KUnit runs: FIFO **46 passed, 2 failed, 3 skipped**;
  fair **49 passed, 2 failed** (these runs preceded the two additional focused
  reset/all-blocked cases and final dispatch diagnostic). The failures are the upstream queue-overhead and
  ping-pong timeout assertions. **Both also fail on the unmodified Arch base**
  under the same UML test config. No full-suite pass or precise cause is claimed.
- Python validator/capture-adjacent tests pass; patch checkpatch has zero errors/
  warnings when the local new-file MAINTAINERS reminder is excluded. Recipe and
  capture shell syntax/recipe metadata checks pass. Actual recipe preparation
  validates sources and both source manifests, then rejects the missing-Rust
  config drift, as intended. Dependency/PGP checks were bypassed only for that
  negative preparation test, not for a production build.

The test host still lacks the production build dependencies, including Rust; a
rootless extracted `bc` was used for the isolated compile checks. Host package
state and boot artifacts were not changed. UML mocks do not exercise AMDGPU
hardware, true GPU timeout/reset, suspend, direct kernel submissions or desktop
presentation. The tests are not evidence that any hardware threshold passes.

## Next handoff

Install/pin the build dependencies and review the accepted full config; build and
inspect the complete side-by-side kernel/package. Prepare/review the distinct
UKI/boot entry and verify stock/default preservation before a one-shot test boot.
Validate real cap/credit/selection traces, errors and recovery; run the required
soak/suspend checks. Implement the solo multi-job and two-victim workload cells,
then freeze the confirmatory capture design. Real-app presentation instrumentation
remains independent. No simulator fitting, policy tuning or fourth policy was
performed.
