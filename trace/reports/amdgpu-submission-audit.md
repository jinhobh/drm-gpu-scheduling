# AMDGPU submission minimum and admission-patch boundary

2026-11-04. Completed the source/history prerequisite in Next 1. This is a
static audit of the pinned Arch base, not proof that scheduler admission one
is safe on hardware. No admission patch, installation or new capture is claimed.

## Decision

Keep `amdgpu.sched_hw_submission`, `ring->num_hw_submission`, physical ring
allocation and hardware fence slots unchanged. Implement the NORMAL/LOW
experiment in scheduler admission with separate per-job reservation state;
implement `global1-gfx` only as a distinct scheduler-credit diagnostic. Do not
remove the driver-wide minimum or treat the global module parameter as a cap knob.

The minimum-two clamp's introducing commit has been found. It specifies two as
the minimum and validates power-of-two input, but gives **no explanation for why
one is unsafe**. The audited code demonstrates allocation dependencies, not an
established hardware requirement that every scheduler maintain two jobs in flight.
Changing scheduler admission while retaining allocations removes that particular
allocation change; it does not establish progress, recovery or performance safety.

## Exact base and verification

| Input | Pin |
|---|---|
| Stable Linux | `v7.2.11`, commit `a2de78ee6ed8bf8cd99d177c74865a6e8c3cb6ea` |
| Arch kernel tag | `v7.2.11-arch1`, tag object `ca35c8c6d827d1d8b75211b41e4ce4062509760a`, commit `64a90897d3c539033f24105968e3513957e70819` |
| Arch packaging | `7.2.11.arch1-1`, commit `0c178d550ec6392265d3a443333229823634bd08` |
| Official stable archive SHA-256 | `fd0fc479f983a8c05313f13e9c36330fe6651cf725396748c40d5cd93fa1e6a4` |
| Arch patch `.zst` SHA-256 | `741d473c4db335d97d9e1d9b8be9337090522550804fe6454ba336d21e580fd6` |
| Arch `config.x86_64` SHA-256 | `f16adcb6dc1dc241ea90e23ca920697d00e4618df1b72d914ff15bc55ab4051f` |

Sources: [stable tag](https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git/tag/?h=v7.2.11),
[Arch tag API](https://api.github.com/repos/archlinux/linux/git/tags/ca35c8c6d827d1d8b75211b41e4ce4062509760a),
[pinned PKGBUILD](https://gitlab.archlinux.org/archlinux/packaging/packages/linux/-/blob/0c178d550ec6392265d3a443333229823634bd08/PKGBUILD),
[pinned config](https://gitlab.archlinux.org/archlinux/packaging/packages/linux/-/blob/0c178d550ec6392265d3a443333229823634bd08/config.x86_64).

Downloaded the official stable `.tar.xz`, Arch release patch and pinned config.
The archive and patch match both SHA-256 and BLAKE2 in the upstream PKGBUILD;
the config matches its BLAKE2. Applied the Arch patch with `patch --batch --fuzz=0`.
It changes only `Makefile`, `amdgpu_dm.c`, `intel_dp.c` and `kernel/fork.c`.
All 14 files in [the checksum manifest](../kernel/audited-files.sha256) match
after patching, including scheduler, ring, fence, job and GC11 source files.
This resolves the downstream-*source* question for these paths; it does not
attest the installed capture binary or reconstruct its compiler/build provenance.
These checks did not verify release signatures; the build recipe preserves
upstream signature checks for the eventual build.

## History: default two versus minimum two

1. [`4afcb30383be`](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=4afcb30383bef8bf972c6aae47995ef314e5f8a1)
   introduced `amdgpu.sched_hw_submission` with default two and passed the value
   into the old AMD scheduler's active queue allocation. That patch did not add
   the current minimum-two validation.
2. [`5d5bd5e32e6e`](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=5d5bd5e32e6e12ab8d844b4b1a1235eb7f00c668),
   authored 2020-01-19, introduced the current clamp in
   `amdgpu_device_check_arguments()`: values below two become two; other
   non-powers-of-two round up. It also replaced the fence initializer's manual
   power-of-two check with `is_power_of_2()`. The commit describes validation
   of user input and states the chosen minimum; it does not derive that minimum
   from fence-slot, ring-space or reset requirements.

Do not confuse this with `5b0112356cf9`, the older power-of-two validation of
**`sched_jobs`**, a different parameter used for context fence indexing.

## Current allocation and submission paths

Line numbers below refer to v7.2.11 and the unchanged audited Arch files.

| Path | What the source establishes | Implementation consequence |
|---|---|---|
| `amdgpu_device.c:1712`, `amdgpu_device_check_arguments` | Global submission parameter is clamped/rounded before ring use | Leave this validation unchanged |
| `amdgpu_ring.c:230`, `amdgpu_ring_init` | Copies parameter into local capacity, then `ring->num_hw_submission`; KIQ uses at least 256, MES uses 8, SDMA page ring uses 256 | Scope the experiment by actual ring type, not every ring or a name substring |
| `amdgpu_ring.c:338` | Physical `ring_size` is rounded from `max_dw * 4 * sched_hw_submission` | Changing the global value changes physical command storage |
| `amdgpu_fence.c:443`, `amdgpu_fence_driver_init_ring` | Requires power-of-two capacity, allocates `2 * num_hw_submission` slots, sets mask to `2 * num_hw_submission - 1` | Preserve storage/mask; the initializer alone does not reject capacity one |
| `amdgpu_fence.c:102`, `amdgpu_fence_emit` | Slot index uses sequence AND mask; reuse may wait for the old fence | Slots serve all hardware submissions, not just capped scheduler jobs |
| `amdgpu_fence.c:159`, polling emit; `mes_v11_0.c:216` | Polling paths use sequence distance derived from the same mask | Scheduler admission must not resize polling capacity |
| `amdgpu_device.c:2377`, `amdgpu_device_init_schedulers` | Skips absent/no-scheduler rings, then assigns `args.credit_limit = ring->num_hw_submission` | Separate scheduler admission here or via explicit scheduler init fields, while retaining physical capacity |

Sources: [device](https://github.com/archlinux/linux/blob/64a90897d3c539033f24105968e3513957e70819/drivers/gpu/drm/amd/amdgpu/amdgpu_device.c),
[ring](https://github.com/archlinux/linux/blob/64a90897d3c539033f24105968e3513957e70819/drivers/gpu/drm/amd/amdgpu/amdgpu_ring.c),
[fence](https://github.com/archlinux/linux/blob/64a90897d3c539033f24105968e3513957e70819/drivers/gpu/drm/amd/amdgpu/amdgpu_fence.c),
[MES11](https://github.com/archlinux/linux/blob/64a90897d3c539033f24105968e3513957e70819/drivers/gpu/drm/amd/amdgpu/mes_v11_0.c).

Repository-wide searches for `num_hw_submission` and `num_fences_mask` under
`drivers/gpu/drm/amd` found the allocation, scheduler-copy and fence consumers
above, plus error/force-completion, debugfs and other MES implementations. They
do not establish an invariant that the scheduler itself must dispatch two jobs.

### Bypasses still exist

`amdgpu_job_submit_direct()` calls `amdgpu_ib_schedule()` without passing through
the scheduler worker. The latter is also called directly by ring IB tests,
including `gfx_v11_0_ring_test_ib()`, and by AMD KFD submission code. KIQ explicitly
does not rely on the GPU scheduler. A scheduler cap therefore bounds only
scheduler-dispatched work. Disabling user queues does not remove every kernel
direct path. The earlier no-user-queue capture result is workload evidence,
not a proof that reset/initialization/direct paths cannot submit.

Sources: [job](https://github.com/archlinux/linux/blob/64a90897d3c539033f24105968e3513957e70819/drivers/gpu/drm/amd/amdgpu/amdgpu_job.c),
[GC11 IB test](https://github.com/archlinux/linux/blob/64a90897d3c539033f24105968e3513957e70819/drivers/gpu/drm/amd/amdgpu/gfx_v11_0.c),
[KFD helper](https://github.com/archlinux/linux/blob/64a90897d3c539033f24105968e3513957e70819/drivers/gpu/drm/amd/amdgpu/amdgpu_amdkfd.c).

## Accounting and recovery requirements for the next patch

The additional cap is a **job count**, independent of total credit units. Save
the dispatch-time class and reservation on the job, so later entity priority
changes, movement or destruction cannot alter the release decision. The source
audit identifies these paths that must be covered before claiming an invariant:

| Path in `sched_main.c` | Existing behavior | Required additional-cap behavior |
|---|---|---|
| `drm_sched_can_queue` (116) | Peeks head; truncates oversized credits to total limit; checks available total credits | Distinguish original total-credit failure from extra-cap failure; do not reserve while probing candidates |
| `drm_sched_run_job_work` (1024) | Pop can return no job; successful pop adds credits before backend `run_job` | Reserve only for a successful dispatch, before backend submission; no charge for failed/dependency-blocked pops |
| `drm_sched_job_done` (163) | Subtracts credits, signals finished fence, schedules free work | Release reservation once, including already-signaled fence and immediate error/null return paths |
| `drm_sched_free_job_work` | Frees completed jobs and queues submission work | Preserve wakeup/progress when the cap becomes free; verify no missed completion wakeups |
| `drm_sched_stop` (395) | Removes callback and subtracts credits only if removal succeeds; otherwise waits for completion | Handle the same callback ownership race for any released reservation |
| `drm_sched_start` (480) | Re-adds credits, reattaches callbacks or completes canceled jobs | Restore/release reservation consistently with the chosen reset semantics; no duplicate decrement |

The generic `stop/start/resubmit_jobs` API is **not the whole AMDGPU recovery
path**. In this base, full-device recovery uses `drm_sched_wqueue_stop/start`
in `amdgpu_device_halt_activities()` / `amdgpu_device_sched_resume()`. The
timeout handler also has soft and per-ring recovery branches. Fence force
completion sets errors and processes hardware fences. Audit these paths against
the chosen reservation lifetime, rather than assuming a generic credit hook
automatically covers the driver.

There is a further exceptional path:
`amdgpu_job_stop_all_jobs_on_sched()` directly signals scheduler fences for
software-queued and pending jobs. Its source labels the helper incorrect and
racy. Direct signaling does not itself call `drm_sched_job_done()`. The patch
needs an explicit, reviewed treatment of this path and later callbacks/freeing;
adding a decrement on every signaled fence would risk double release. Do not
silently reset a live reservation counter to zero as a recovery shortcut.

Sources: [scheduler main](https://github.com/archlinux/linux/blob/64a90897d3c539033f24105968e3513957e70819/drivers/gpu/drm/scheduler/sched_main.c),
[driver job/recovery helpers](https://github.com/archlinux/linux/blob/64a90897d3c539033f24105968e3513957e70819/drivers/gpu/drm/amd/amdgpu/amdgpu_job.c),
[device recovery](https://github.com/archlinux/linux/blob/64a90897d3c539033f24105968e3513957e70819/drivers/gpu/drm/amd/amdgpu/amdgpu_device.c).

### Selection and observable invariants

Stock FIFO visits priority queues in priority order. Fair uses a single tree;
`drm_sched_rq_select_entity()` returns `-ENOSPC` at a ready head that cannot fit.
Use separate failure reasons so `skip-cap` skips only the extra-cap failure and
still stops for insufficient original total credits. Preserve the protocol's
fair/off/stock, fair/bg1/stock and fair/bg1/skip-cap distinctions. No sleeper
accounting change belongs in this patch. See
[the pinned selection source](https://github.com/archlinux/linux/blob/64a90897d3c539033f24105968e3513957e70819/drivers/gpu/drm/scheduler/sched_rq.c).

Before hardware measurement, trace scheduler/ring identity, job identity, saved
class, reservation acquire/release reason, outstanding count, original credits,
skip reason and examined nodes. Check at every transition that combined
NORMAL/LOW scheduler-dispatched unfinished occupancy is at most one, original
credits remain bounded and HIGH/KERNEL acquire no extra-cap reservation.
Test a cap-blocked NORMAL head ahead of ready HIGH, all candidates blocked,
immediate/error completion, entity destruction, priority change and recovery.
Still require the protocol's solo multi-job, two-victim and throughput gates.

## Delivered scaffold and remaining work

[The package recipe and handoff](../kernel/README.md) preserve Arch's pinned
config and sources under `linux-drmsched`; expected release is
`7.2.11-arch1-1-drmsched`. Recipe syntax/metadata, archive/patch checksums,
zero-fuzz patch application, all 14 source hashes, rootless Kconfig generation
and the release suffix were checked. Kconfig silently dropped Rust with the
current missing toolchain; the recipe now fails on config drift. `makepkg`
correctly refuses preparation because `bc`, `rust`, `rust-bindgen` and
`rust-src` are absent. This is not a kernel/module compile or package build.

Next: install build dependencies through the user's root terminal, implement
and review the admission/selection/lifecycle patch and tracing on this base,
compile, finish the distinct UKI/boot handoff, then boot and perform recovery/
suspend checks. Package installation, stock/default-boot preservation checks
and the hardware experiment remain undone. Simulator and policies are unchanged.

Follow-up (2026-11-04): [the admission/selection prototype](gfx-admission-implementation.md)
is now implemented and mock/compile-tested. The paragraph above records the audit's
original handoff; production package/boot and real-device validation are still pending.
