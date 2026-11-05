#!/usr/bin/env python3
"""Kernel-side wait until GPU start for a burst victim, the hog and Hyprland (read-only).

    python3 trace/start_wait.py RUN... [--ahead]

Needs <run>/jobs.csv from parse.py. Wait = sched_wait + ring_wait (ready -> HW start), so
capped runs (which wait in the scheduler) and uncapped runs (which wait in the ring) are
comparable; it does not use burst_client's GPU timestamp correction. The hog is the
burst_client gfx client with the longest median exec; the victim is the next one with
> 100 jobs. Hyprland = its HIGH (prio 1) gfx jobs.

--ahead counts, for each victim job, hog jobs dispatched while the victim was ready but
not yet dispatched, and how many of those were queued before the victim.
"""
import argparse
import collections
import csv
import statistics


def q(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))]


def load(run):
    rows = [r for r in csv.DictReader(open(f"{run}/jobs.csv")) if r["ring"] == "gfx_0.0.0"]
    by = collections.defaultdict(list)
    for r in rows:
        by[(r["comm"], r["client"], r["prio"])].append(r)
    bc = sorted((k for k in by if k[0].startswith("burst_client")),
                key=lambda k: -statistics.median(float(r["exec"]) for r in by[k]))
    if len(bc) < 2:
        raise ValueError(f"{run}: need a hog and a victim burst_client")
    hog = bc[0]
    victim = next(k for k in bc[1:] if len(by[k]) > 100)
    hypr = [r for k in by if k[0].startswith("Hyprland") and k[2] == "1" for r in by[k]]
    return by[hog], by[victim], hypr


def wait(r):
    return float(r["sched_wait"]) + float(r["ring_wait"])


def t_run(r):
    return float(r["t_queue"]) + (float(r["dep_wait"]) + float(r["order_wait"])
                                  + float(r["sched_wait"])) / 1e3


def ahead(hog, victim):
    runs = sorted((t_run(h), float(h["t_queue"])) for h in hog)
    total, older = collections.Counter(), collections.Counter()
    for v in victim:
        q0, r0 = float(v["t_queue"]), t_run(v)
        a = [h for h in runs if q0 < h[0] < r0]
        total[len(a)] += 1
        older[sum(1 for h in a if h[1] < q0)] += 1
    return dict(sorted(total.items())), dict(sorted(older.items()))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--ahead", action="store_true")
    args = ap.parse_args()
    print("run victim_p50 victim_p99 hypr_p50 hypr_p99 hog_exec_p50 victim_jobs (ms)")
    for run in args.runs:
        run = run.rstrip("/")
        hog, victim, hypr = load(run)
        v, h = [wait(r) for r in victim], [wait(r) for r in hypr]
        print(f"{run} {statistics.median(v):.2f} {q(v, .99):.2f} "
              f"{statistics.median(h):.2f} {q(h, .99):.2f} "
              f"{statistics.median(float(r['exec']) for r in hog):.2f} {len(v)}")
        if args.ahead:
            total, older = ahead(hog, victim)
            print(f"  hog jobs dispatched ahead: {total}; of which queued before victim: {older}")


if __name__ == "__main__":
    main()
