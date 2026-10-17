#!/usr/bin/env python3
"""Diagnostics for one (policy, scenario, seed) run.  FROZEN.

    python3 tools/inspect_run.py --scenario game_widgets
    python3 tools/inspect_run.py --scenario launch_hog --seed 2 --timeline 4.0:4.06
    python3 tools/inspect_run.py --scenario mimic_attack --policy baselines/fair.py

This tool may show ground truth (client names/classes, true costs) to help you form
hypotheses.  Your policy still cannot see any of it at runtime.
"""

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gpusim import metrics, scenarios  # noqa: E402
from gpusim.api import PRIO_NAMES  # noqa: E402
from gpusim.runner import run  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", default=os.path.join(ROOT, "policy.py"))
    ap.add_argument("--scenario", required=True, choices=list(scenarios.SCENARIOS))
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--split", default="dev", choices=["dev", "holdout"])
    ap.add_argument("--trusted", action="store_true")
    ap.add_argument("--timeline", help="seconds window START:END to dump gfx/sdma jobs, e.g. 3.5:3.55")
    args = ap.parse_args()

    m, sim = run(args.policy, args.scenario, args.seed, split=args.split, trusted=args.trusted, keep_sim=True,
                 trace=bool(args.timeline))
    sc = scenarios.SCENARIOS[args.scenario]
    print(f"# {args.scenario} seed={args.seed} split={args.split}  ({sc['desc']})")
    print(f"refresh={sim.cfg['refresh_hz']}Hz period={sim.display.period}us lead={sim.display.lead}us "
          f"vram={sim.cfg['vram_mb']}MB hints={sim.cfg['client_deadline_hints']}")
    s, br = metrics.score_run(m, None, sc["checks"])
    miss = {k: (f"{100 * v:.1f}%" if v is not None else "-") for k, v in m["miss"].items()}
    print(f"jank={br['jank']:.2f} (tput ratio needs score.py)  miss={miss}")
    print(f"gfx util={m['gfx_util']:.2f} sdma util={m['sdma_util']:.2f}  evictions={m['evictions']} "
          f"({m['evicted_mb']:.0f} MB) moved_in={m['moved_in_mb']:.0f} MB  "
          f"max wait normal={m['wait_normal_ms']:.1f}ms low={m['wait_low_ms']:.1f}ms")
    if m["mimic_share"] is not None:
        print(f"mimic share={m['mimic_share']:.3f}")
    if m["jain"] is not None:
        print(f"jain(throughput clients)={m['jain']:.3f}")
    if m["app_ttff_ms"]:
        print("app time-to-first-frame ms:", [round(x) for x in m["app_ttff_ms"]])

    print()
    print(f"{'client':16s} {'class':11s} {'ents (ring:prio:id)':30s} {'jobs':>6s} {'gpu%':>6s} "
          f"{'frames':>6s} {'miss%':>6s} {'maxwait':>8s} {'gttBOs':>7s}")
    span = max(1, sim.end - sim.warmup)
    for c in sim.clients:
        if not c.ents:
            continue
        ents = " ".join(f"{e.ring}:{PRIO_NAMES[e.priority][0]}:{e.id}" for e in c.ents)
        pc = m["per_client"].get(c.name)
        bos = [b for b in sim.mem.bos if b.client is c]
        gtt = f"{sum(1 for b in bos if b.dom == 'G')}/{len(bos)}" if bos else "-"
        print(f"{c.name:16s} {c.cls:11s} {ents[:30]:30s} {c.jobs_done:6d} {100 * c.gpu_time / span:6.1f} "
              f"{(pc[1] if pc else 0):6d} {(100 * pc[0] if pc else 0):6.1f} {c.max_wait / 1000:7.1f}ms {gtt:>7s}")

    if m["first_late_ms"]:
        fl = sorted(m["first_late_ms"])
        print(f"\nfirst-frame lateness vs ideal (ms): n={len(fl)} median={fl[len(fl) // 2]:.1f} "
              f"p90={fl[int(len(fl) * 0.9)]:.1f} max={fl[-1]:.1f}")

    if args.timeline:
        a, b = (int(float(x) * 1e6) for x in args.timeline.split(":"))
        d = sim.display
        frame_of = {rec.job.id: rec for rec in sim.frames if rec.job is not None}
        print(f"\n# timeline {a / 1e6:.3f}s..{b / 1e6:.3f}s  (ms; vblank every {d.period / 1000:.2f}ms, "
              f"compositor latches clients {d.lead / 1000:.2f}ms before vblank)")
        print(f"{'ring':4s} {'client':16s} {'tag':7s} {'cost':>6s} {'submit':>9s} {'ready':>9s} {'run':>9s} "
              f"{'start':>9s} {'done':>9s} {'ideal':>9s} {'late':>4s} {'deadline':>9s}")
        f = lambda t: f"{t / 1000:9.2f}" if t is not None else f"{'-':>9s}"
        for j in sim._trace:
            if j.done_us < a or j.submit_us > b:
                continue
            rec = frame_of.get(j.id)
            late = "-"
            if rec is not None:
                idx = d.vblank_idx if rec.grid == "vblank" else d.latch_idx
                late = str(idx(j.done_us) - idx(j._ideal))
            print(f"{j.ring:4s} {j._client.name:16s} {j._tag:7s} {j._cost / 1000:6.2f} {f(j.submit_us)} "
                  f"{f(j.ready_us)} {f(j.run_us)} {f(j._start)} {f(j.done_us)} {f(j._ideal)} {late:>4s} "
                  f"{f(j.deadline_us)}")


if __name__ == "__main__":
    main()
