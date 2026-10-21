#!/usr/bin/env python3
"""Summarize a burst_client frame log (CSV from trace/burst_client).

    python3 trace/burst_stats.py LOG.csv            # per frame-in-burst latencies
    python3 trace/burst_stats.py LOG.csv --by-idle  # calibration: after-idle vs warm

LOG may also be a capture directory containing burst.csv.

Per frame (all from the log, CLOCK_MONOTONIC):
  start_lat  gpu_start - t_submit    (queueing + scheduling + GPU wake-up)
  exec       gpu_end - gpu_start     (clocks / power state)
  to_pres    presented - gpu_end     (compositor latch + scanout)
  latency    presented - t_intended
  skipped    vblanks lost between consecutive presented frames of a burst (msc gap - 1)

--by-idle groups bursts by the idle gap before them and compares frame 0 (and 1) with the
warm frames (the second half of each burst) of the same bursts.
"""

import argparse
import collections
import csv
import os
import statistics
import sys


def load(path):
    if os.path.isdir(path):
        path = os.path.join(path, "burst.csv")
    with open(path) as f:
        header = [l.rstrip() for l in f if l.startswith("#")]
    with open(path) as f:
        rows = list(csv.DictReader(l for l in f if not l.startswith("#")))
    for r in rows:
        for k, v in r.items():
            if k != "state":
                r[k] = float(v) if "." in v else int(v)
    return header, [r for r in rows if r["burst"] >= 0]


def ms(a, b):
    return (a - b) / 1e6


def q(xs, p):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))]


def med(xs):
    return statistics.median(xs) if xs else float("nan")


def per_frame(rows):
    by_burst = collections.defaultdict(list)
    for r in rows:
        by_burst[r["burst"]].append(r)
    cols = collections.defaultdict(lambda: collections.defaultdict(list))
    for frames in by_burst.values():
        frames.sort(key=lambda r: r["frame"])
        prev = None
        for r in frames:
            c = cols[r["frame"]]
            c["start_lat"].append(ms(r["gpu_start"], r["t_submit"]))
            c["exec"].append(r["exec_us"] / 1e3)
            if r["state"] == "presented":
                c["to_pres"].append(ms(r["presented"], r["gpu_end"]))
                c["latency"].append(ms(r["presented"], r["t_intended"]))
                if prev is not None:
                    c["skipped"].append(r["msc"] - prev["msc"] - 1)
                prev = r
            else:
                c["discarded"].append(1)
    print("frame   n  start_lat p50/p99   exec p50/p99   to_pres p50/p99  latency p50/p99"
          "  skipped  discarded   (ms)")
    for i in sorted(cols):
        c = cols[i]
        n = len(c["exec"])
        print(f"{i:5d} {n:3d}  {med(c['start_lat']):6.2f} {q(c['start_lat'], .99):6.2f}"
              f"   {med(c['exec']):5.2f} {q(c['exec'], .99):5.2f}"
              f"   {med(c['to_pres']):6.2f} {q(c['to_pres'], .99):6.2f}"
              f"   {med(c['latency']):6.2f} {q(c['latency'], .99):6.2f}"
              f"   {sum(c['skipped']):6d}  {len(c['discarded']):6d}")


def by_idle(rows):
    groups = collections.defaultdict(lambda: collections.defaultdict(list))
    n_frames = max(r["frame"] for r in rows) + 1
    for r in rows:
        g = groups[r["idle_ms"]]
        key = r["frame"] if r["frame"] < 2 else "warm" if r["frame"] >= n_frames // 2 else None
        if key is None:
            continue
        g[f"exec{key}"].append(r["exec_us"] / 1e3)
        g[f"start{key}"].append(ms(r["gpu_start"], r["t_submit"]))
    print(f"warm = frames {n_frames // 2}..{n_frames - 1} of the same bursts; times in ms")
    print("idle_ms  bursts  exec0  exec1  warm  exec0/warm   start0  start1  start_warm")
    for idle in sorted(groups):
        g = groups[idle]
        w = med(g["execwarm"])
        print(f"{idle:7d}  {len(g['exec0']):6d}  {med(g['exec0']):5.3f}  {med(g['exec1']):5.3f}"
              f"  {w:5.3f}  {med(g['exec0']) / w:9.3f}   {med(g['start0']):6.3f}"
              f"  {med(g['start1']):6.3f}  {med(g['startwarm']):6.3f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("log")
    ap.add_argument("--by-idle", action="store_true")
    a = ap.parse_args()
    header, rows = load(a.log)
    print("\n".join(header))
    if not rows:
        sys.exit("no burst rows")
    (by_idle if a.by_idle else per_frame)(rows)


if __name__ == "__main__":
    main()
