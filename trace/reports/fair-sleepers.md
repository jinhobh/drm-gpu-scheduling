# Upstream fair and the after-idle vkmark stall

Source and capture study, 2026-11-04; plan.md Next 1.

Fair can repeatedly reward a tiny-job flood whose **software queue empties** even
while it keeps the GPU busy. The observed ~15.5 ms maximum is not a scheduler
latency guarantee. Policy 3 needs bounded sleeper credit based on accounted GPU
work, plus aging of waiting work and control of in-flight jobs.

## Source inspected

The pinned upstream stable tag is `v7.2.11`, matching the captures' base kernel
version (`7.2.11-arch1-1`). These are upstream sources, not a reconstruction of
Arch's complete build tree; local headers do not include the scheduler C files.

- [sched_rq.c](https://github.com/gregkh/linux/blob/v7.2.11/drivers/gpu/drm/scheduler/sched_rq.c):
  save/restore/update vruntime, head priority, and selection.
- [sched_entity.c](https://github.com/gregkh/linux/blob/v7.2.11/drivers/gpu/drm/scheduler/sched_entity.c):
  runtime accounting, queue push/pop, and priority mapping.
- [sched_main.c](https://github.com/gregkh/linux/blob/v7.2.11/drivers/gpu/drm/scheduler/sched_main.c):
  credits, job freeing, and fence timestamp correction.
- [gpu_scheduler.h](https://github.com/gregkh/linux/blob/v7.2.11/include/drm/gpu_scheduler.h):
  EWMA declaration and scheduler state.

SHA-256 of the downloaded `sched_rq.c`:
`634142eb99ad9a0110cb1c2fcd5b3944a3548dd05422f19730c30939242480d6`.

## Wake-up and accounting

Fair combines priorities in one tree. `drm_sched_rq_update_prio()` caches the
tree head's priority, including a head that is not ready. On software-queue
exhaustion, the entity saves only positive vruntime lag behind the remaining
head. Sleep does not decay that debt. At re-entry, zero-lag entities receive a
priority/average-job-size adjustment relative to the head; same-priority short
jobs go 1 ns ahead, longer jobs 1 ns behind. Lower-priority re-entry gets a
duration-derived penalty. None of this tests how long the entity slept.

The important accounting detail is when vruntime advances: only a pop **with
another job still queued** folds accumulated runtime into it. Empty-queue pops
save relative position instead. Repeated one-job submissions can therefore keep
rejoining ahead without charging their accumulated GPU work to their tree key.
There is no wait-age promotion or fixed 15 ms cap in this path.

`drm_sched_entity_stats_job_add_gpu_time()` adds the scheduled-to-finished fence
duration to cumulative runtime and the entity EWMA. The free worker also updates
the scheduler EWMA. Before freeing a pending job, it adjusts its successor's
scheduled timestamp to the previous finished timestamp, reducing charged ring
queueing. This is delayed software accounting, not a GPU timestamp measurement.
It must not be replaced with instantaneous completion-time vruntime updates in
an exact fair model.

## Why the two hogs differ

The burst victim also submits one job at a time. Against a heavy vk-hog, its
average job is short enough for favorable same-priority placement; it can get
the next available credit ahead of a queued hog job. Already submitted hog
work still runs, so frame-0 median improves (8.44 ms vs FIFO 14.2 ms), while the
steady-frame tail stays near two hog jobs.

Against vkmark's ~0.03–0.05 ms jobs, the burst victim's ~1 ms job is relatively
long. More importantly, vkmark can repeatedly empty its software queue and
return ahead of an already waiting victim. Queue exhaustion does not mean GPU
idleness: two jobs may still be in flight. Thus a job flood benefits from the
same heuristic intended to favor intermittent interactive clients.

Selection checks readiness in tree order and then checks credits. If the first
ready entity cannot fit, selection stops with `-ENOSPC`; it does not reserve a
future credit or try later entities. The trace's `credit` component measures
time when two jobs were in flight. It does **not** prove that ordering was
irrelevant: repeatedly giving each freed credit to vkmark keeps that component
large. This differs from unavoidable blocking behind two long jobs already
submitted before the victim became ready.

## Capture evidence and the apparent ceiling

Reproduce without root, using existing `frames.csv` joins:

```sh
python3 trace/fair_sleepers.py traces/m3-p2/auto-vkmark-cube-r*/*/
```

The script reads the raw trace, locates frame-0 waits, and checks whether each
overtaking vkmark pop had its successor queued by the run tracepoint. That
tracepoint slightly follows the pop, making the empty-queue count conservative.
It also reports queued flood work at victim release, the next vkmark push, and
the nearest HIGH Hyprland submission relative to release.

For example, in r2 (`20261019-142843`):

| burst | start latency (ms) | overtaking vkmark pops | empty after pop | nearest HIGH submit after victim run (ms) |
|---|---:|---:|---:|---:|
| 3 | 15.092 | 308 | 308 | 0.053 |
| 7 | 15.388 | 325 | 325 | 0.191 |
| 9 | 15.469 | 325 | 325 | 0.089 |

Across all three runs, 21/60 frame-0s waited at least 5 ms (correcting the earlier
manual count of 18). All 5,120 vkmark pops during those waits exhausted the
software queue. All ten waits exceeding 15 ms end with no queued vkmark work;
the nearest HIGH submission follows victim run by 0.048–0.191 ms.

For bursts 3 and 7, no vkmark job was queued at victim release; the next pushes
arrived 183 and 289 us later. A CPU submission gap let the victim run. Burst 9
likewise had its next push 164 us later. The preceding HIGH submissions were
about 1.2–1.7 ms before victim submission, one refresh before the next ones.
The longest stalls consequently consume almost the remaining 60 Hz refresh
interval (~16.67 ms minus ~1.2 ms).

This supports a workload/compositor-periodic interruption of the flood as the
explanation for the apparent ceiling. It does not establish what causes the
CPU submission gap: these captures do not record vkmark's CPU scheduling or
swapchain waits. They also lack internal vruntime/EWMA and exact free-worker
timestamps, so the precise restore branch at every insertion cannot be proved.
The source supplies a starvation mechanism, and the traces show its required
queue-exhaustion pattern; they do not supply a complete internal-state replay.
Do not fit a hard 15 ms bound into phase 2.

## Consequences for phases 2 and 3

- Preserve cumulative GPU accounting across empty software queues. A tiny-job
  flood must not get fresh sleeper credit on each push. Bound wake-up credit
  and retain anti-gaming limits based on sustained GPU use/per-frame cost.
- Age ready work so repeated re-entry cannot postpone it indefinitely. A bound
  on sleeper credit alone does not bound an older victim's wait.
- Keep the policy's one-NORMAL/LOW-job-in-flight mechanism: ordering cannot
  reclaim already submitted non-preemptible hog work.
- Model upstream fair separately, including delayed free-worker accounting,
  pop-with-successor updates, save/restore, head-priority placement, fixed-point
  microsecond EWMAs, and readiness/credit stopping. Validate both the tiny-job
  flood regression and the heavy-hog frame-0 improvement.

The frozen `baselines/fair.py` updates vruntime
at completion and adds completion cost to sleeping lag. That approximates fair
but misses the empty-pop accounting path above. Leave the simulator and policies frozen until the
phase-2 versioning step; old scores must remain comparable.
