#!/usr/bin/env python3
"""Compare matrices (e.g. one per sched_policy) per hog, after join.py has written frames.csv.

    python3 trace/compare.py traces/m3-p1 traces/m3-p2 traces/m3-p0

A MATRIX is a matrix.sh output dir; several dirs can be pooled under one label with
LABEL=DIR,DIR (e.g. p1=traces/m3-p1,traces/m3-p1b). Per hog and matrix (repeats pooled):
start latency (gpu_start - t_submit) p50/p99 for frame 0 and frames 1+, presented -
intended p50 (f0) and p50/p99 (f1+), Hyprland's ms per frame on the ring while the frame
waited, and the vk-hog's fps and job time (from hog.csv, burst_client hogs only).
"""

import collections
import csv
import glob
import os
import re
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import burst_stats  # noqa: E402

ORDER = ["none", "vkmark-cube", "vk-hog1", "vk-hog", "gl-hog", "glmark2-terrain", "calib"]


def q(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else float("nan")


def run_stats(d):
    fr = list(csv.DictReader(open(os.path.join(d, "frames.csv"))))
    _, br = burst_stats.load(d)
    pres = lambda f: [(r["presented"] - r["t_intended"]) / 1e6 for r in br
                      if f(r["frame"]) and r["state"] == "presented"]
    s = dict(f0=[float(o["start_lat"]) for o in fr if o["frame"] == "0"],
             f1=[float(o["start_lat"]) for o in fr if o["frame"] != "0"],
             hypr=[float(t.split("=")[1]) for o in fr for t in o["busy_by"].split()
                   if t.startswith("Hyprland")],
             p0=pres(lambda f: f == 0), p1=pres(lambda f: f > 0))
    h = os.path.join(d, "hog.csv")
    if os.path.exists(h) and os.path.getsize(h):
        # the hog's frames have burst -1, which burst_stats.load drops
        with open(h) as f:
            hr = list(csv.DictReader(l for l in f if not l.startswith("#")))
        t = [int(r["gpu_end"]) for r in hr]
        s["fps"] = (len(t) - 1) / ((t[-1] - t[0]) / 1e9)
        s["hog_ms"] = statistics.median(float(r["exec_us"]) for r in hr) / 1e3
    return s


def main():
    groups = collections.defaultdict(list)
    labels = []
    for arg in sys.argv[1:]:
        label, _, dirs = arg.rpartition("=")
        dirs = dirs.split(",")
        label = label or os.path.basename(dirs[0].rstrip("/"))
        labels.append(label)
        for m in dirs:
            for d in sorted(glob.glob(os.path.join(m, "*", "*", ""))):
                if not os.path.exists(os.path.join(d, "frames.csv")):
                    continue
                hog = re.sub(r"-r\d+$", "", os.path.basename(os.path.dirname(d.rstrip("/"))))
                hog = re.sub(r"^(auto|high|high-nogfxoff)-", "", hog)
                s = run_stats(d)
                if s["f0"] or s["f1"]:  # a failed run leaves an empty frames.csv
                    groups[hog, label].append(s)
    if not groups:
        sys.exit("no runs with frames.csv (run trace/join.py first)")
    print(f"{'hog':12} {'matrix':10} runs | start f0 p50   p99  f1+ p50   p99 | presented f0 "
          f"f1+ p50   p99 | hypr ms/fr | hog fps    hog ms")
    hogs = sorted({h for h, _ in groups}, key=lambda h: (ORDER + [h]).index(h))
    for hog in hogs:
        for label in labels:
            rs = groups.get((hog, label))
            if not rs:
                continue
            cat = lambda k: [x for r in rs for x in r[k]]
            n = len(cat("f0")) + len(cat("f1"))
            fps = [r["fps"] for r in rs if "fps" in r]
            ms = [r["hog_ms"] for r in rs if "hog_ms" in r]
            print(f"{hog:12} {label:10} {len(rs):4} | {q(cat('f0'), .5):12.2f} "
                  f"{q(cat('f0'), .99):5.2f} {q(cat('f1'), .5):8.2f} {q(cat('f1'), .99):5.2f} | "
                  f"{q(cat('p0'), .5):12.1f} {q(cat('p1'), .5):7.1f} {q(cat('p1'), .99):5.1f} | "
                  f"{sum(cat('hypr')) / max(n, 1):10.2f} | "
                  + (f"{min(fps):5.1f}-{max(fps):5.1f} {min(ms):4.2f}-{max(ms):4.2f}"
                     if fps else ""))


if __name__ == "__main__":
    main()
