#!/usr/bin/env python3
"""Per-victim frame cost for the self-cost cells (read-only after join.py).

    python3 trace/selfcost.py RUN...

For each burst_client log in RUN (burst.csv, or burst-a.csv and burst-b.csv from
workloads/two_victims.sh) and its join.py output (frames.csv, frames-burst-a.csv, ...), one
row per client with medians over frames (ms unless stated):

    span      kernel-side first job queued -> last job done (`span_trace`); the protocol's
              self-cost scalar is the run median of this, per client
    exec      summed kernel-side exec of the frame's jobs (`exec_trace_sum`)
    between   time between one of the frame's jobs ending and the next starting
    f0_wait   frame 0's first job, ready -> HW start (pick + credit + ring_wait)
    jobs      kernel jobs matched per frame (must equal --jobs) and the client's log setting
    pres      share of frames presented; skip = vblanks skipped between consecutive
              presented frames of a burst, per frame; fps = delivered frames per second
              within bursts (median over bursts)

Frames whose window size differs from the client's most common size are dropped and counted
(`drop`): two tiled victims settle into their tiles during the first frames.
Join first: `python3 trace/join.py RUN` and `python3 trace/join.py RUN --log burst-a.csv`.
"""
import argparse
import collections
import csv
import glob
import os
import statistics

import burst_stats


def med(xs):
    return statistics.median(xs) if xs else float("nan")


def frames_path(run, log):
    return os.path.join(run, "frames.csv" if log == "burst.csv" else "frames-" + log)


def client(run, log):
    header, rows = burst_stats.load(os.path.join(run, log))
    with open(frames_path(run, log), newline="") as f:
        joined = {(int(r["burst"]), int(r["frame"])): r for r in csv.DictReader(f)}
    sizes = collections.Counter((r["ext_w"], r["ext_h"]) for r in rows)
    modal = sizes.most_common(1)[0][0]
    keep = [r for r in rows if (r["ext_w"], r["ext_h"]) == modal]
    out = dict(run=run, log=log, frames=len(keep), drop=len(rows) - len(keep),
               log_jobs=sorted({r.get("jobs", 1) for r in rows}))
    span, exec_sum, between, f0, n_jobs = [], [], [], [], collections.Counter()
    for r in keep:
        j = joined.get((r["burst"], r["frame"]))
        if not j or j["ok"] != "1" or not j.get("span_trace"):
            continue
        n_jobs[int(j["n_jobs"])] += 1
        span.append(float(j["span_trace"]))
        exec_sum.append(float(j["exec_trace_sum"]))
        between.append(float(j["between_trace"]))
        if r["frame"] == 0:
            f0.append(float(j["pick"]) + float(j["credit"]) + float(j["ring_wait"]))
    out.update(matched=len(span), span=med(span), exec=med(exec_sum), between=med(between),
               f0_wait=med(f0), jobs=dict(sorted(n_jobs.items())))
    by_burst = collections.defaultdict(list)
    for r in keep:
        by_burst[r["burst"]].append(r)
    presented = skipped = 0
    fps = []
    for fr in by_burst.values():
        p = sorted((r for r in fr if r["state"] == "presented"), key=lambda r: r["msc"])
        presented += len(p)
        skipped += sum(max(0, b["msc"] - a["msc"] - 1) for a, b in zip(p, p[1:]))
        if len(p) > 1 and p[-1]["presented"] > p[0]["presented"]:
            fps.append((len(p) - 1) / ((p[-1]["presented"] - p[0]["presented"]) / 1e9))
    out.update(pres=presented / len(keep) if keep else float("nan"),
               skip=skipped / len(keep) if keep else float("nan"), fps=med(fps))
    return out


def logs(run):
    names = sorted(os.path.basename(p) for p in glob.glob(os.path.join(run, "burst*.csv")))
    return [n for n in names if n == "burst.csv" or n.startswith("burst-")]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="+")
    args = ap.parse_args()
    print("run log frames drop matched span exec between f0_wait jobs log_jobs pres skip fps")
    for run in args.runs:
        run = run.rstrip("/")
        for log in logs(run):
            c = client(run, log)
            print(f"{run} {log} {c['frames']} {c['drop']} {c['matched']} {c['span']:.3f} "
                  f"{c['exec']:.3f} {c['between']:.3f} {c['f0_wait']:.3f} "
                  f"{c['jobs']} {c['log_jobs']} {c['pres']:.3f} {c['skip']:.3f} {c['fps']:.1f}")


if __name__ == "__main__":
    main()
