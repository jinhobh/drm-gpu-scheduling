#!/usr/bin/env python3
"""Score a scheduling policy.  FROZEN — the optimization agent must not edit this file.

    python3 score.py                      # score ./policy.py on the dev suite
    python3 score.py --policy baselines/fair.py
    python3 score.py --scenario game_widgets --seeds 1   # quick look (not a valid score)
    python3 score.py --json out.json      # also dump everything as JSON

Prints a per-scenario table and a final block:

    jank_score:   <float>   lower is better (see gpusim/metrics.py for the definition)
    valid:        true|false

`valid` is false if the run was partial (--scenario/--seeds) or the policy crashed.
The throughput constraint is relative to stock FIFO; its per-run reference numbers
are computed once and cached in .cache/.
"""

import argparse
import hashlib
import json
import os
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from gpusim import metrics, scenarios  # noqa: E402
from gpusim.runner import run  # noqa: E402

REF_POLICY = os.path.join(ROOT, "baselines", "fifo.py")


def _sim_hash():
    h = hashlib.sha256()
    for fn in sorted(os.listdir(os.path.join(ROOT, "gpusim"))):
        if fn.endswith(".py"):
            with open(os.path.join(ROOT, "gpusim", fn), "rb") as f:
                h.update(fn.encode())
                h.update(f.read())
    with open(REF_POLICY, "rb") as f:
        h.update(f.read())
    return h.hexdigest()[:16]


def _job(args):
    policy, scen, seed, split, trusted = args
    try:
        m = run(policy, scen, seed, split=split, trusted=trusted)
        return (scen, seed, m, None)
    except Exception:
        return (scen, seed, None, traceback.format_exc())


def _run_many(policy, pairs, split, trusted, workers):
    tasks = [(policy, s, seed, split, trusted) for s, seed in pairs]
    if workers <= 1:
        return [_job(t) for t in tasks]
    with ProcessPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(_job, tasks))


def reference(split, pairs, workers):
    """FIFO useful work per (scenario, seed), cached on disk."""
    path = os.path.join(ROOT, ".cache", f"ref-{split}-{_sim_hash()}.json")
    ref = {}
    if os.path.exists(path):
        with open(path) as f:
            ref = json.load(f)
    missing = [(s, seed) for s, seed in pairs if f"{s}:{seed}" not in ref]
    if missing:
        for scen, seed, m, err in _run_many(REF_POLICY, missing, split, True, workers):
            if err:
                raise RuntimeError(f"reference run failed:\n{err}")
            ref[f"{scen}:{seed}"] = m["useful_work"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(ref, f, indent=1, sort_keys=True)
    return ref


def evaluate(policy, split="dev", only=None, seeds=None, trusted=False, workers=None):
    names = list(scenarios.SCENARIOS) if not only else only
    for n in names:
        if n not in scenarios.SCENARIOS:
            raise SystemExit(f"unknown scenario {n!r}; have: {', '.join(scenarios.SCENARIOS)}")
    if seeds is None:
        seeds = scenarios.DEV_SEEDS if split == "dev" else scenarios.HOLDOUT_SEEDS
    pairs = [(n, s) for n in names for s in seeds]
    workers = workers or min(len(pairs), os.cpu_count() or 1)
    ref = reference(split, pairs, workers)
    results = _run_many(policy, pairs, split, trusted, workers)
    errors = [(s, seed, err) for s, seed, m, err in results if err]
    per = {}
    for scen, seed, m, err in results:
        if err:
            continue
        sc = scenarios.SCENARIOS[scen]
        score, br = metrics.score_run(m, ref.get(f"{scen}:{seed}"), sc["checks"])
        per.setdefault(scen, []).append((score, br, m))
    return names, per, errors


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _fmt(x, w=7, p=2):
    return f"{x:{w}.{p}f}" if x is not None else f"{'-':>{w}}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--policy", default=os.path.join(ROOT, "policy.py"))
    ap.add_argument("--split", default="dev", choices=["dev", "holdout"])
    ap.add_argument("--scenario", action="append", help="run only this scenario (repeatable)")
    ap.add_argument("--seeds", type=lambda s: tuple(int(x) for x in s.split(",")), help="e.g. 1,2")
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--trusted", action="store_true", help="skip realism checks (calibration only)")
    ap.add_argument("--json", help="write full results to this file")
    args = ap.parse_args()

    t0 = time.time()
    try:
        names, per, errors = evaluate(args.policy, args.split, args.scenario, args.seeds,
                                      args.trusted, args.workers)
    except Exception as ex:  # policy rule violations, import errors
        print(f"ERROR: {ex}")
        print("---")
        print("jank_score:   nan")
        print("valid:        false")
        sys.exit(1)

    if errors:
        scen, seed, err = errors[0]
        print(f"CRASH in {scen} seed {seed}:\n{err}")
        print("---")
        print("jank_score:   nan")
        print("valid:        false")
        sys.exit(1)

    hdr = (f"{'scenario':16s} {'score':>7s} {'jank':>7s} {'burst%':>7s} {'resp%':>7s} {'comp%':>7s} "
           f"{'steady%':>7s} {'tput':>6s} {'fair':>6s} {'mimic':>6s} {'waitN':>7s} {'waitL':>7s} {'penalty':>7s}")
    print(hdr)
    print("-" * len(hdr))
    totals = []
    jank_all = []
    pen_all = []
    out = {}
    for n in names:
        rows = per.get(n, [])
        if not rows:
            continue
        sc = _mean([r[0] for r in rows])
        jk = _mean([r[1]["jank"] for r in rows])
        pn = _mean([r[1]["penalty"] for r in rows])
        miss = {c: _mean([None if r[2]["miss"][c] is None else 100 * r[2]["miss"][c] for r in rows])
                for c in metrics.CLASS_W}
        tput = _mean([r[1]["tput_ratio"] for r in rows])
        fair = _mean([r[2]["jain"] for r in rows]) if "fair" in scenarios.SCENARIOS[n]["checks"] else None
        mim = _mean([r[2]["mimic_share"] for r in rows])
        wn = max(r[2]["wait_normal_ms"] for r in rows)
        wl = max(r[2]["wait_low_ms"] for r in rows)
        totals.append(sc)
        jank_all.append(jk)
        pen_all.append(pn)
        print(f"{n:16s} {_fmt(sc)} {_fmt(jk)} {_fmt(miss['burst'])} {_fmt(miss['resp'])} {_fmt(miss['comp'])} "
              f"{_fmt(miss['steady'])} {_fmt(tput, 6)} {_fmt(fair, 6, 3)} {_fmt(mim, 6, 3)} {_fmt(wn, 7, 1)} "
              f"{_fmt(wl, 7, 1)} {_fmt(pn)}")
        out[n] = dict(score=sc, jank=jk, penalty=pn, miss=miss, tput_ratio=tput, jain=fair,
                      mimic_share=mim, wait_normal_ms=wn, wait_low_ms=wl,
                      runs=[dict(seed=r[2]["seed"], score=r[0], **{k: v for k, v in r[1].items()},
                                 app_ttff_ms=r[2]["app_ttff_ms"], evictions=r[2]["evictions"],
                                 gfx_util=r[2]["gfx_util"], per_client=r[2]["per_client"])
                             for r in rows])

    complete = not args.scenario and not args.seeds
    final = _mean(totals)
    print("---")
    print(f"jank_score:   {final:.6f}")
    print(f"jank_pct:     {_mean(jank_all):.6f}")
    print(f"penalty:      {_mean(pen_all):.6f}")
    print(f"split:        {args.split}")
    print(f"valid:        {'true' if complete else 'false (partial run)'}")
    print(f"eval_seconds: {time.time() - t0:.1f}")
    if args.json:
        with open(args.json, "w") as f:
            json.dump(dict(jank_score=final, split=args.split, valid=complete, scenarios=out), f, indent=1)


if __name__ == "__main__":
    main()
