#!/usr/bin/env python3
"""Inspect frame-0 starvation and flood queue occupancy in existing joined captures.

Usage: python3 trace/fair_sleepers.py RUN... [--min-ms 5]
Reads frames.csv and trace.dat; writes nothing. Pop is approximated by the run
tracepoint immediately following it. A successor queued between pop and run can
therefore make an empty queue look nonempty. No vruntime is recorded in these traces.
"""

import argparse
import bisect
import csv
from pathlib import Path

import join


def inspect(run, minimum):
    jobs = join.load_jobs(run)
    gfx = [j for j in jobs.values() if j.ring.startswith("gfx")]
    flood = sorted((j for j in gfx if join.client_name(j) == "vkmark"),
                   key=lambda j: j.t_run)
    times = [j.t_run for j in flood]
    compositor = sorted(j.t_queue for j in gfx
                        if join.client_name(j) == "Hyprland" and j.prio == 1)
    with (run / "frames.csv").open() as f:
        frames = list(csv.DictReader(f))
    first = [r for r in frames if r["frame"] == "0"]
    slow = [r for r in first if float(r["start_lat"]) >= minimum]
    print(f"{run}: {len(slow)}/{len(first)} frame-0s >= {minimum:g} ms")
    print("burst start_ms pops empty_after_pop queued_at_release next_push_us comp_queue_delta_ms")
    for r in slow:
        victim = jobs[int(r["ctx"]), int(r["seq"])]
        lo, hi = victim.t_ready, victim.t_run
        overtakes = flood[bisect.bisect_left(times, lo):bisect.bisect_left(times, hi)]
        empty = sum((n := jobs.get((j.ctx, j.seq + 1))) is None or n.t_queue > j.t_run
                    for j in overtakes)
        queued = sum(j.t_queue <= hi < j.t_run for j in flood)
        future = [j.t_queue for j in flood if j.t_queue > hi]
        next_push = f"{(min(future) - hi) * 1e6:.0f}" if future else "-"
        nearest = min(compositor, key=lambda t: abs(t - hi)) if compositor else None
        delta = f"{(nearest - hi) * 1e3:.3f}" if nearest is not None else "-"
        print(f"{r['burst']:>5} {float(r['start_lat']):8.3f} {len(overtakes):4} "
              f"{empty:15} {queued:17} {next_push:>12} {delta:>19}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--min-ms", type=float, default=5)
    args = parser.parse_args()
    for run in args.runs:
        inspect(run, args.min_ms)


if __name__ == "__main__":
    main()
