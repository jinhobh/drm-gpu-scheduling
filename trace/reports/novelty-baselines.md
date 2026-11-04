# Related work and kernel baseline specification

Source review, 2026-11-04; completes plan.md's novelty/baseline step.
This specifies future experiments; no kernel policy was implemented or tested here.

The first comparison uses one identical build: FIFO off/bg1 with stock selection,
and fair off/stock, bg1/stock and bg1/skip-cap. Admission and selection are distinct
factors: fair's stock walk stops at a cap-blocked ready entity, so skipping it
changes effective service order. HIGH/KERNEL are exempt from the additional
NORMAL/LOW cap, which also includes interactive victims. See the
[updated protocol](kernel-limit-protocol.md) for per-victim self-cost gates and
the larger fair implementation scope. The all-priority global limit specified
below remains a distinct diagnostic; the full policy is conditional on hardware
results. **Upstream v7.2.11 already contains the published vruntime race fix.**
The general repeated-re-entry unfairness problem was also described upstream
before our study. Neither queue-depth limiting nor that problem is a novelty
claim. A possible contribution remains automatic in-flight control on unmodified
desktop clients, if it improves presentation and the latency–throughput tradeoff
beyond the simple limit and applicable fair fixes.

## Related work and implications

| Primary source | Relevant mechanism | Consequence for this project |
|---|---|---|
| [Linux V3D scheduling documentation](https://docs.kernel.org/gpu/v3d.html#gpu-scheduling) | Submits the next hardware job after the previous completes, explicitly to protect interactive latency from bulk work. | A global one-job limit is established practice. The AMD/Hyprland measurement and any adaptive advantage must carry the contribution. |
| [TimeGraph, USENIX ATC 2011, §§4–5](https://www.usenix.org/events/atc11/tech/final_files/Kato.pdf) | PRT dispatches at GPU-idle boundaries; HT permits additional same-task work when no higher-priority task waits. Reservations and execution-cost prediction regulate interference. | Both conservative dispatch and the throughput tradeoff have prior art. Compare against a fixed limit before claiming automatic classification or prediction is needed. TimeGraph's explicit priorities/reserves differ from inferring desktop interactivity. |
| [GPUSync, authors' paper](https://www.cs.unc.edu/~anderson/papers/rtss13c.pdf) | Treats GPU management as synchronization, with configurable priorities, parallel resources and budget policing. | Fairness, priority inversion and GPU isolation are established objectives; our target is transparent drm_sched control of ordinary graphics clients, not a new general isolation concept. |
| [GCAPS, authors' preprint](https://arxiv.org/abs/2406.05221) | Driver-level, priority-based context preemption with application macros on Nvidia Jetson. | Hardware/driver preemption is another way to reduce blocking. Our current model controls dispatch boundaries and cannot interrupt a running long job. |
| [GPreempt, USENIX ATC 2025](https://www.usenix.org/conference/atc25/presentation/fan) | Timeslice-based context-switch preemption with hints to reduce overhead. | Do not claim low-latency sharing or preemption as new. This is a different intervention; it need not be ported to this AMD machine for the minimum experiment. |
| [Upstream fair introduction](https://github.com/torvalds/linux/commit/2fa4d8e2c1091189064c5b93222a23ded2d881ba) and [interactive placement adjustment](https://github.com/torvalds/linux/commit/af85fa14ad7c4adf4276c65bb640843c6d210927) | Runtime-weighted fairness and preferential placement for short/bursty entities. | Sleeper preference and runtime fairness are existing mechanisms. Any benefit must survive a fair baseline and the tiny-job flood control. |

This is a focused survey sufficient to select baselines, not an exhaustive novelty
clearance. The candidate empirical claim is whether transparent dispatch control
improves **first presentation and missed refreshes** for real clients while
retaining throughput. A paper must explain the difference from existing
admission control and reservations, and remove policy components that do not
improve that tradeoff.

## Exact source pins and fix status

Upstream stable sources matching the capture version:

- Repository: `https://github.com/gregkh/linux.git`.
- Annotated tag `v7.2.11`: tag object
  `8316c24ec1b77dba19ba072ca4fe251b36dc65d3`, peeled commit
  [`a2de78ee6ed8bf8cd99d177c74865a6e8c3cb6ea`](https://github.com/gregkh/linux/commit/a2de78ee6ed8bf8cd99d177c74865a6e8c3cb6ea).
- Fair introduction: `2fa4d8e2c1091189064c5b93222a23ded2d881ba`;
  interactive adjustment: `af85fa14ad7c4adf4276c65bb640843c6d210927`.
- Published locking fix: upstream
  [`2ab510e63197360945f915dd5631a77c63ac6b27`](https://github.com/torvalds/linux/commit/2ab510e63197360945f915dd5631a77c63ac6b27),
  stable backport
  [`e89900c441fec90644baf49c3f46ce76d91c51e4`](https://github.com/gregkh/linux/commit/e89900c441fec90644baf49c3f46ce76d91c51e4).
  The latter is already in v7.2.11's scheduler history and its locking is present
  in the downloaded source. **Do not cherry-pick it again.**
- Current mainline snapshot checked:
  [`05441d697f7f9a927264f4058cbbc6a03dfcac9d`](https://github.com/torvalds/linux/commit/05441d697f7f9a927264f4058cbbc6a03dfcac9d)
  (committed 2026-11-03). Its `sched_rq.c` is byte-identical to v7.2.11;
  `sched_entity.c` has subsequent lifecycle/locking changes, including
  `0e118b936dc5904cf0d9859882a40c96057b083d` (idle check) and
  `4b7a49d66c8398a7984dff4ac54d1acc6b3a58df` (`last_scheduled`). This
  review found no merged replacement of the empty-pop accounting/placement path
  in that snapshot. This is a dated finding, not a promise about future upstream.

The captures identify `7.2.11-arch1-1`; that version string alone does not prove
Arch's exact downstream source or binary contents. Phase 3 must retain the Arch
PKGBUILD/source checksums, downstream patches, config and built module hashes.
Until then, “v7.2.11 contains the fix” is an upstream-source result, not a binary
attestation of the laptop's module.

The [source manifest](novelty-baselines-sources.json) records SHA-256 hashes of
the six inspected stable files and pinned history queries. Recheck the stable
source bytes without root from the repository root:

```sh
python3 - <<'CHECK'
import hashlib, json, urllib.request
from pathlib import Path
m = json.loads(Path("trace/reports/novelty-baselines-sources.json").read_text())
for path, expected in m["stable"]["files"].items():
    url = "https://raw.githubusercontent.com/gregkh/linux/" + m["stable"]["commit"] + "/" + path
    actual = hashlib.sha256(urllib.request.urlopen(url).read()).hexdigest()
    if actual != expected:
        raise SystemExit("source mismatch: " + path)
    print("verified", path)
CHECK
```

### Three distinct issues

1. **Push/pop locking race, fixed.** Queue exhaustion could become visible to a
   concurrent push before conversion of absolute vruntime to saved relative lag.
   Restoring that unsaved value could move an entity far backwards in service
   order. The merged fix extends the entity lock across queue mutation and tree
   management. It does not change which pop branches account runtime.
2. **Repeated-re-entry unfairness, already discussed upstream.** The
   [2026-08-14 proposal](https://www.mail-archive.com/dri-devel@lists.freedesktop.org/msg628299.html)
   addressed a continuously queued entity falling behind entities that leave and
   rejoin. Its diff also moved runtime updating before the successor test, directly
   overlapping the accounting path implicated by our study. The author
   [dropped that proposal for further work](https://www.mail-archive.com/dri-devel@lists.freedesktop.org/msg628326.html)
   the same day. The series' reported development commit
   `840e24cc75ac9a836c633b23cb95dda0e0dc3b75` is **not a verified merged
   upstream commit or a selected baseline patch**. Do not apply the withdrawn
   diff as a validated fix or call the general issue newly discovered here.
3. **Our capture-supported mechanism.** [The sleeper report](fair-sleepers.md)
   connects 5,120 overtaking empty-queue pops to 21 long frame-0 waits. Source
   inspection shows the successor-only runtime update remains after the locking
   fix. This is evidence in a specific desktop workload, not proof of every
   internal branch, a new bug class, or a 15 ms scheduling bound.

“Fixed fair” therefore currently means **v7.2.11 fair with its already merged
race fix**, not a second policy configuration. If a reviewed accounting or
placement fix becomes available before hardware evaluation, pin and test it on
all applicable arms. An independently written accounting repair must be labeled
as our experimental repair, with race, debt preservation and fairness checks;
it must not be presented as upstream fair.

## All-priority global one-job diagnostic: normative specification

The [updated experiment protocol](kernel-limit-protocol.md) selects `bg1-gfx`
with HIGH/KERNEL exempt for the first experiment. The definition below is still
valid for `global1-gfx`, but is not the specification of that priority-exempt cap.

Name the experimental arm `fifo-global1-gfx`. “Global” means shared by **all
entities using the measured gfx scheduler**, not one job per client and not one
job across unrelated GPU engines. Record the gfx ring(s) used. If clients use
multiple gfx schedulers, a per-ring limit is not a device-wide limit; report that
scope or design a separate shared admission mechanism before making such a claim.

- Use FIFO (`gpu_sched.sched_policy=1`) with the same dependencies, priority
  selection and job ordering as the uncapped FIFO arm.
- Set the selected gfx scheduler's `drm_sched_init_args.credit_limit` to **1**
  at initialization. AMDGPU currently initializes it from
  `ring->num_hw_submission` in
  [amdgpu_device_init_schedulers()](https://github.com/gregkh/linux/blob/v7.2.11/drivers/gpu/drm/amd/amdgpu/amdgpu_device.c).
  An opt-in test-kernel setting should override only that argument for gfx;
  preserve the hardware ring allocation and other engine settings. Default off.
- AMDGPU's [job allocation](https://github.com/gregkh/linux/blob/v7.2.11/drivers/gpu/drm/amd/amdgpu/amdgpu_job.c)
  passes one credit per scheduler job. Verify this for every submission path
  exercised. With those unit credits, existing credit accounting enforces one
  scheduler-dispatched, unfinished job at a time. This counts dispatched jobs,
  including ring wait, not just executing commands or software-queued jobs.
- **No priority exemptions:** LOW, NORMAL, HIGH and KERNEL all consume the same
  single credit on that scheduler. HIGH/KERNEL retain FIFO's strict priority
  selection when the credit becomes available; they cannot preempt a dispatched
  job. Hyprland is capped too. A NORMAL/LOW-only cap with spare HIGH/KERNEL
  capacity is a distinct ablation, not this global-one-job baseline.
- Dependency-blocked software jobs consume no credit. Use the existing
  [scheduler completion/credit path](https://github.com/gregkh/linux/blob/v7.2.11/drivers/gpu/drm/scheduler/sched_main.c)
  for completion, errors and reset recovery; do not add a duplicate counter or
  release a credit merely because an entity's software queue emptied.
- SDMA, compute and video schedulers keep their original limits. Direct kernel
  submissions bypassing drm_sched and user queues are outside the guarantee;
  run with user queues disabled and audit direct submissions during validation.
- Do not change `policy.py`, classify clients, predict deadlines, add holds,
  alter vruntime or require a cooperative client in this arm. Do not use a
  driver-wide submission parameter as a substitute without checking its scope.

This baseline can leave a CPU dispatch bubble at every job boundary and can
reduce compositor throughput. Those are measured costs. It bounds the number
of jobs ahead, not job duration, total waiting time or starvation. An arbitrarily
long current job still blocks a new interactive frame; strict priorities can
still starve lower classes.

### Implementation acceptance checks (phase 3, not executed here)

Record the resolved ring, credit limit, policy, base/patch/config hashes and user
queue setting. Test unmodified clients, two independent hogs and mixed priorities.
Trace dispatch/completion and verify at most one unfinished job on each capped
scheduler, including HIGH/KERNEL; report direct-path work separately. Check
completion errors, canceled jobs, dependency chains, timeout/reset and
suspend/resume for credit leaks and forward progress. Confirm other engines keep
their limits. Keep the daily stock kernel and default boot entry untouched.

## Minimum comparisons and decision rule

| Arm | Ordering and admission | Purpose |
|---|---|---|
| `fifo/off/stock` | FIFO, original gfx credits and selection | FIFO control. |
| `fifo/bg1/stock` | FIFO, combined NORMAL/LOW admission cap one | Cap effect with stock priority selection. |
| `fair/off/stock` | Fair, original credits and selection; merged race fix present | Fair control. |
| `fair/bg1/stock` | Fair, NORMAL/LOW cap one, stock stop-at-blocked-head selection | Cap-only diagnostic including possible HIGH blocking. |
| `fair/bg1/skip-cap` | Fair, NORMAL/LOW cap one, skip only extra-cap-blocked candidates | Combined cap and modified selection; compare to capped stock for selection effect. |
| `fifo-global1-gfx` | FIFO, one gfx credit shared by every priority | Separate all-priority admission diagnostic. |
| `policy3` (conditional) | Selected policy, explicit admission/selection semantics | Added value beyond simpler interventions. |

Before a full-policy claim, also compare its NORMAL/LOW cap without its other
mechanisms against the all-priority limit. Otherwise the advantage might simply
come from exempting compositor/kernel work. Add `fair-global1-gfx` if results
suggest an ordering × depth interaction, and a reviewed fair accounting repair
when available. RR and the client-side `vk-hog1` remain diagnostic controls.

Use identical binaries/settings except the selected intervention, independent
sessions and counterbalanced policy order, one verified active display, matched
window geometry, and the pacing instrumentation specified in plan.md. Cover
real launches/scrolling/animations, heavy GL/Vulkan hogs, tiny-job floods, two
hogs, a short-job mimic, video and CPU-only contention. Include no-hog solo
multi-job victims and two independent NORMAL interactive clients with aligned
and offset bursts; gate per-client execution, inter-job span, frame latency and
missed refreshes against matched uncapped cells. Report first-frame
presentation, missed refreshes, GPU wait, throughput, fairness, maximum wait
and overhead with run-level uncertainty. Compare at matched throughput cost.
A kernel change has not succeeded on presentation merely because GPU wait fell.

Prepare the fair bug report first after checking drm-misc-next. Proceed with the
kernel fixed-limit experiment and real-app presentation instrumentation as
independent tracks. Implement the fixed limit before expanding the full policy. If it explains the benefit, the result
is a narrower measurement/control study; the simulator score and a known fair
pathology do not establish novelty for the full scheduler.
