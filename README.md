# drm-gpu-scheduling — agentic search for a BORE-inspired fair drm_sched policy

Goal: an AMD APU with a small VRAM carveout (this machine: Ryzen AI 7 350, Radeon 860M,
512 MiB carveout, kernel 7.0 with drm_sched `sched_policy=1` FIFO) should absorb
**spontaneous burst loads** — widget animations, liquid-glass style interactions, app
launches with heavy first frames — smoothly, even while something else is hogging the GPU.

An agent iterates on one file, `policy.py`, a drm_sched entity-selection policy. A frozen,
deterministic simulator scores each attempt in ~1–3 s. The agent's loop is in
[program.md](program.md), modelled on Karpathy's *autoresearch*: one editable file, one
scalar, keep or `git reset`, repeat.

## The training objective

**`jank_score` (lower is better) = mean over 10 scenarios × 5 seeds of `jank + penalty`.**

`jank` is the weighted percentage of interactive frames that **missed the vblank they would
have hit on an otherwise idle GPU** — so only *contention-caused* misses count, and a frame
that is inherently too heavy is never charged to the scheduler.

| class | weight | what it measures | why |
|---|---:|---|---|
| `burst` | 0.35 | frames of widget / scroll / glass / app-intro animations (incl. frames dropped because 2 were already in flight) | smoothness of the bursts you care about |
| `resp` | 0.30 | first frame after an input or app launch, graded `min(vblanks late, 4)/4` | perceived responsiveness / launch latency |
| `comp` | 0.20 | compositor frames that missed vblank | a compositor miss stutters the whole screen |
| `steady` | 0.15 | video and capped-game frames | bursts must not be bought by breaking steady streams |

Constraints are hinges that cost nothing while satisfied, so the agent is free to trade
anything *inside* them:

| constraint | rule | penalty | catches |
|---|---|---|---|
| work conservation | total useful GPU work ≥ 0.90 × stock FIFO's, same run | `200·max(0, 0.90−T)` | policies that "win" by idling the GPU |
| GPU-time fairness | Jain ≥ 0.95 among backlogged equal-priority hogs | `200·max(0, 0.95−J)` | job-count fairness (FIFO/RR) |
| anti-gaming | disguised hog's share of contended GPU ≤ 0.55 | `200·max(0, S−0.55)` | BORE's classic weakness: sleep-briefly-to-look-interactive |
| no starvation | ready→run wait ≤ 250 ms (NORMAL+), ≤ 1 s (LOW) | up to 50 each | strict priority starving LOW |

Why work conservation is measured against FIFO's *total* useful work (not the hog's own
throughput): a good policy renders the interactive frames FIFO was dropping. That GPU time
legitimately comes out of the hog, and it shouldn't be punished. Only *wasted* capacity
should be: holding the ring idle, thrashing VRAM.

## Calibration

| policy | dev | holdout | notes |
|---|---:|---:|---|
| `baselines/fifo.py` | 88.0 | 84.1 | stock default on this kernel |
| `baselines/rr.py` | 78.5 | 74.8 | legacy round robin, starves LOW |
| `baselines/fair.py` | **59.9** | 54.5 | upstream CFS-style "fair" approximation, the agent's starting `policy.py` |
| `baselines/bore_naive.py` | 46.3 | 43.9 | direct BORE port: jank 39%, but the mimic takes 74% of the GPU |
| `baselines/oracle_trusted.py` | 37.7 | 42.9 | cheats: sees the future and true costs. A hint of headroom, not a bound |

Without the work-conservation constraint the oracle drives jank to ~0–5% by starving hogs.
The interesting problem is the trade-off, not whether jank can be removed.

## Layout

```
policy.py                 the ONLY file the agent edits (starts as baselines/fair.py)
program.md                agent instructions + experiment loop
score.py                  frozen scorer: prints per-scenario table, jank_score, valid
tools/inspect_run.py      per-client diagnostics + job timeline for one run
baselines/                fifo, rr, fair, bore_naive, oracle_trusted (calibration only)
gpusim/api.py             everything a policy can see (read this first)
gpusim/engine.py          drm_sched + amdgpu APU model (rings, fences, VRAM/TTM)
gpusim/workloads.py       compositor, animators, games, compute, mimic, app launches
gpusim/scenarios.py       the 10-scenario suite + holdout perturbations
gpusim/metrics.py         objective definition
gpusim/loader.py          kernel-realism sandbox for policies
```

## Running it

```bash
python3 score.py                                   # score policy.py
```

```bash
python3 score.py --policy baselines/bore_naive.py  # score any policy file
```

```bash
python3 tools/inspect_run.py --scenario game_widgets --timeline 3.56:3.59
```

Start the agent from this directory, e.g. a Claude Code session with the prompt
*"Read program.md and start a new run."* It works on branch `opt/<tag>` and logs every
experiment to `results.tsv` (untracked).

Your job as the human, periodically:

```bash
python3 score.py --split holdout
```

Compare dev against holdout. A widening gap means the agent is overfitting the dev seeds.
Also read `policy.py`'s docstring and diff for anything that isn't kernel-plausible.

## What the simulator models

* **drm_sched semantics**: per-entity FIFO queues, head-only selection, dependency fences
  (same-scheduler deps wait only for the *scheduled* fence), `credit_limit = 2`
  (`amdgpu.sched_hw_submission`), scheduler work-item latency.
* **HW ring**: in-order and non-preemptive (no MCBP on the kernel-queue path). Doorbell and
  fetch latency when feeding an idle ring, context-switch cost between clients. gfx and SDMA
  share DRAM bandwidth.
* **VRAM**: 512 MB carveout, TTM-like LRU eviction. Evictions are pipelined SDMA copies that
  wait for *every* job still using the victim BO, queued or running. So a hog's deep queue
  delays the VRAM a newly launched app needs, a priority inversion visible through
  `e.waiters`. GTT-resident BOs run 30% slower. Optional moves back to VRAM are throttled
  like `amdgpu_cs_get_threshold_for_moves` (`amdgpu.moverate` = 8 MB/s).
* **Display**: 60–144 Hz vblank grid. The compositor (HIGH priority) latches client buffers
  `0.4·frame` before vblank, renders, and must finish by vblank. Its fences carry deadline
  hints; client fences carry them only in some scenarios.
* **Fair comparisons**: every random draw is indexed by vblank/frame/burst number on
  per-client streams. Frame N of the game costs the same under every policy.
* **Anti-cheating**: entity and client ids are random per run, entity order is shuffled,
  and policies are AST-checked (no `_private` access, restricted imports and builtins).

### Known gaps (sim → real)

Not modelled: GPU clock ramp-up after idle (DPM), which matters a lot for "spontaneous"
bursts on APUs; compute rings and MES/user queues (they bypass drm_sched); mutter/KWin's
adaptive max-render-time; partial-frame overlap between consecutive jobs; real CS ioctl
costs. Costs are plausible for an 860M, not measured. The next step for fidelity is to
record real traces on this machine (drm_sched/dma_fence tracepoints via perfetto or
`trace-cmd`, plus `wp_presentation` feedback) and fit `scenarios.py` to them.

### Path to the kernel

The API mirrors drm_sched on purpose: `select()` ↔ `drm_sched_select_entity`, hooks ↔
`push_job / run_job / free_job / entity_init`, `ps` ↔ new fields in `drm_sched_entity` /
`drm_sched_entity_stats`, `deadline_us` ↔ `dma_fence_set_deadline` (already propagated by
`drm_sched_fence`). A winning policy becomes a `sched_policy=2` option in `gpu-sched.ko`
(a module on this kernel), built out-of-tree against the 7.0 headers and A/B tested on this
APU against `sched_policy=1`.
