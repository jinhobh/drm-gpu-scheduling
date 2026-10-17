# program.md — evolve a BORE-inspired fair drm_sched policy

You are an autonomous research agent. Your job: make `python3 score.py` print the lowest
possible `jank_score` by editing **only `policy.py`**, a drm_sched entity-selection policy
that runs inside a simulator of an AMD APU (Radeon 860M, 512 MiB VRAM carveout).

The target design is a **BORE-inspired fair GPU scheduler**: fair like upstream's CFS-style
"fair" drm_sched policy, but strongly favouring entities that behave interactively
(short GPU bursts separated by idle time), without being gameable and without wasting GPU.

## The objective (read `gpusim/metrics.py` for the exact definition)

`jank_score` = mean over 10 scenarios × 5 seeds of `jank + penalty`. **Lower is better.**

* `jank` (0–100) is the weighted % of interactive frames that missed the vblank they
  would have hit on an otherwise idle GPU:
  `burst` 0.35 (widget, scroll, glass, app-intro frames), `resp` 0.30 (first frame after
  input or app launch, graded by vblanks late, capped at 4), `comp` 0.20 (compositor
  frames, which stutter the whole screen), `steady` 0.15 (video, capped game).
* `penalty` is zero while the constraints hold:
  * **work conservation**: total useful GPU work ≥ 90% of stock FIFO's on the same run
    (holding the ring idle costs this; giving hog time to interactive frames does not)
  * **fairness**: Jain index ≥ 0.95 over GPU time of backlogged equal-priority clients (`fair_hogs`)
  * **anti-gaming**: a hog disguised as interactive gets ≤ 55% of the contended GPU (`mimic_attack`)
  * **no starvation**: ready→run wait ≤ 250 ms for KERNEL/HIGH/NORMAL jobs, ≤ 1 s for LOW jobs

Reference points on the dev suite:

| policy | jank_score | notes |
|---|---:|---|
| `baselines/fifo.py` | 88.0 | stock 7.0 kernel default |
| `baselines/rr.py` | 78.5 | legacy round robin |
| `baselines/fair.py` | 59.9 | upstream "fair" approximation, **your starting point** |
| `baselines/bore_naive.py` | 46.3 | strawman BORE port: gameable (mimic share 0.74) |
| `baselines/oracle_trusted.py` | 37.7 | cheats (sees future and true costs) — not a bound, just a hint that much more is possible |

## Setup (once)

1. Pick a run tag from today's date (e.g. `oct16`). `git checkout -b opt/<tag>` from `main`.
   The branch must not exist yet.
2. Read, in order: `README.md`, `gpusim/api.py` (everything your policy can see),
   `gpusim/metrics.py` (the objective), `gpusim/scenarios.py` and `gpusim/workloads.py`
   (what you are evaluated on), `baselines/*.py`, `policy.py`. Skim `gpusim/engine.py` for the
   HW model (in-order non-preemptive ring, 2 credits, doorbell latency, context switch
   cost, shared DRAM bandwidth, VRAM eviction chains).
3. Create `results.tsv` (untracked) with this header (tab-separated):
   `commit	jank_score	jank_pct	penalty	status	description`
4. Run the baseline: `python3 score.py > run.log 2>&1` and log it as `keep`.

## Rules

* **Edit only `policy.py`.** Never modify `gpusim/`, `score.py`, `tools/`, `baselines/`.
* The loader enforces kernel realism: allowed imports are math, heapq, bisect, collections,
  itertools, functools, dataclasses, typing, gpusim.api; no `_private` attributes except on
  `self`; no getattr/eval/open/id/hash; attribute writes only on `self.*`, `x.ps.*`,
  `x.client_ps.*`. Don't try to get around it — such a policy can't exist in the kernel.
* **Never** special-case scenarios, entity/client ids, seeds, or recognise a scenario by
  fingerprinting its timing. Ids are random every run; the human scores your final policy on
  a **holdout split** with different seeds, refresh rates (60–144 Hz), VRAM sizes and cost
  scales. Overfitting shows up there. **Do not run `--split holdout`.**
* Keep it portable to C in `drivers/gpu/drm/scheduler/`: O(#entities) work per decision,
  bounded per-entity state (no unbounded lists; fixed-size rings or EWMAs), arithmetic that
  maps to integers/fixed point (log2 ≈ fls, shifts, EWMAs). Every mechanism should map to
  something the kernel has or could plumb: fence timestamps, entity queue state, fence
  deadline hints, dependency fences.
* **Simplicity criterion**: all else equal, simpler is better. A 0.1 gain that adds 40
  lines of special cases is not worth it; deleting code at equal score is a win. Keep the
  module docstring an accurate description of the algorithm and its kernel mapping.

## The experiment loop

LOOP FOREVER:

1. Look at the current state: last `run.log` table, `results.tsv`, `git log --oneline -5`.
2. Pick ONE hypothesis. Write it as a one-line commit-style description first.
3. Edit `policy.py`. `git commit -am "<description>"`.
4. Run: `python3 score.py > run.log 2>&1` (takes ~1–3 s; kill anything over 2 minutes).
5. Read the result: `grep "^jank_score:\|^valid:" run.log` and the per-scenario table.
   If it crashed or violated a rule, `tail -n 30 run.log`, fix obvious mistakes and re-run;
   give up on the idea after a few attempts.
6. Append a row to `results.tsv`: short commit hash, jank_score, jank_pct, penalty,
   `keep`/`discard`/`crash`, description.
7. If `jank_score` went down (by ≥ 0.05, or equal with simpler code): keep the commit.
   Otherwise: `git reset --hard HEAD~1`.
8. Every ~10 experiments, dig into the worst scenario:
   `python3 tools/inspect_run.py --scenario <name> --timeline <t0>:<t0+0.03>` shows
   per-client miss rates and a job timeline (with ground truth your policy can't see), and
   is the fastest way to find *why* frames miss. Use it to choose the next hypotheses.

Do not pause to ask the human whether to continue; they may be away. If you run out of
ideas, re-read the engine and the scenario descriptions, combine near-misses, try the
opposite of what failed, or simplify. Run until you are interrupted.

## Background: BORE, and how it maps onto drm_sched

BORE (Burst-Oriented Response Enhancer, a CFS/EEVDF patch by firelzrd) tracks per task the
CPU time consumed since it last slept ("burst time"), converts it into a penalty
`max(0, log2(burst) - offset) * scale`, smooths it (rises slowly, drops fast), and shifts the
task's effective nice level by it. Short-burst (interactive) tasks win wakeup races; hogs
sink. Children inherit an averaged penalty of their parent's family so fork storms can't
reset it, and kernel threads can be excluded.

GPU mapping to start from (ideas, not a recipe):

| BORE | drm_sched analogue |
|---|---|
| task sleeps | entity goes idle (`entity_idle`, `e.idle_since_us`) |
| burst time | GPU time (`j.gpu_us`) consumed since the entity last went idle |
| nice-level shift | vruntime weight shift (upstream fair uses `gpu_us << shift[prio]`) |
| burst cache / fork inheritance | client-level history in `e.client_ps`, shared by the client's entities; app launches create brand-new clients with no history |
| wakeup preemption | no GPU preemption! levers are pick order, HW queue depth, and deliberate holds |

Levers you have:

1. **Pick order** in `select()` (fair vruntime, burst penalty, priority, deadlines).
2. **HW queue depth**: the ring takes 2 jobs and never preempts. A second hog job queued
   behind the first is pure head-of-line latency for the next interactive frame.
3. **Anticipatory holds**: return `None` and `sched.request_wakeup(t)` to keep the ring free
   for predicted arrivals. Compositor jobs carry `deadline_us` = vblank, which reveals the
   vblank grid; interactive entities wake periodically. Holding idles the GPU and costs
   work conservation.
4. **Deadline hints** (`j.deadline_us`, `fence_deadline`): EDF-style urgency. Present on
   compositor jobs always, on client frames only in some scenarios.
5. **Priority inheritance**: in VRAM pressure, a new app's uploads wait on TTM eviction
   copies, which wait on the hog's queued jobs. `e.waiters` / `j.blocked_on` expose these chains.
6. **The SDMA scheduler** runs the same policy class: TTM moves (KERNEL) vs app uploads (NORMAL).

Known traps: a hog that sleeps 1 ms between jobs looks "interactive" to naive burst
tracking (`mimic_attack`); job-count fairness is not GPU-time fairness (`fair_hogs`);
strict priority starves LOW (`priority_mix`); holding too eagerly fails work conservation;
`desktop_bursts` has no load at all and must stay near ~4.
