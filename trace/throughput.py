#!/usr/bin/env python3
"""Steady gfx throughput per client from parse.py's jobs.csv (read-only).

    python3 trace/throughput.py RUN... [--skip S] [--tail S] [--min-jobs N]

For each gfx_0.0.0 client with at least --min-jobs jobs: jobs per second over its own active
window, dropping the first --skip seconds (warm-up, default 3) and the last --tail seconds
(shutdown, default 1), plus median job exec (ms) and the share of that window its jobs
executed on the GPU (busy). The protocol's throughput cost is log(off/on) of the hog's
jobs/s between paired arms: vk-hog (one job per frame) in the burst cells, and the flood
client in workloads/flood.sh cells.
"""
import argparse
import collections
import csv
import statistics


def rates(run, skip=3.0, tail=1.0, min_jobs=200):
    with open(f"{run}/jobs.csv", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["ring"] == "gfx_0.0.0"]
    by = collections.defaultdict(list)
    for r in rows:
        by[(r["comm"].split(":")[0], r["client"], r["prio"])].append(r)
    out = []
    for (comm, client, prio), js in by.items():
        if len(js) < min_jobs:
            continue
        t = sorted(float(r["t_queue"]) for r in js)
        lo, hi = t[0] + skip, t[-1] - tail
        if hi <= lo:
            continue
        win = [r for r in js if lo <= float(r["t_queue"]) < hi]
        if not win:
            continue
        busy = sum(float(r["exec"]) for r in win) / 1e3 / (hi - lo)
        out.append(dict(comm=comm, client=client, prio=prio, jobs=len(win),
                        seconds=hi - lo, rate=len(win) / (hi - lo),
                        exec=statistics.median(float(r["exec"]) for r in win), busy=busy))
    return sorted(out, key=lambda c: -c["rate"] * c["exec"])


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--skip", type=float, default=3.0)
    ap.add_argument("--tail", type=float, default=1.0)
    ap.add_argument("--min-jobs", type=int, default=200)
    args = ap.parse_args()
    print("run comm client prio jobs seconds jobs_per_s exec_p50_ms busy")
    for run in args.runs:
        run = run.rstrip("/")
        for c in rates(run, args.skip, args.tail, args.min_jobs):
            print(f"{run} {c['comm']} {c['client']} {c['prio']} {c['jobs']} "
                  f"{c['seconds']:.1f} {c['rate']:.1f} {c['exec']:.3f} {c['busy']:.3f}")


if __name__ == "__main__":
    main()
