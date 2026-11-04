#!/usr/bin/env python3
"""Compare burst pacing with Hyprland submissions and hardware vblanks (read-only).

    python3 trace/pacing.py RUN... [--csv frames.csv]

Times are ms. t_present brackets a client API call, not the compositor's receipt
of a Wayland commit. Hyprland submissions cannot be attributed to a surface or
output. Select the output by presentation feedback MSC AND timestamp, never by
CRTC number. No GPU timestamp correction is needed for the primary metrics.
"""

import argparse
import bisect
import collections
import csv
import re
import statistics
import subprocess

import burst_stats
import parse


def events(run):
    hypr, vblanks = [], collections.defaultdict(list)
    proc = subprocess.Popen(["trace-cmd", "report", "-i", f"{run}/trace.dat"],
                            stdout=subprocess.PIPE, text=True)
    for line in proc.stdout:
        m = parse.LINE.match(line)
        if not m:
            continue
        if m["ev"] == "drm_sched_job_queue" and m["task"].startswith("Hyprland"):
            if "ring=gfx" in m["body"]:
                hypr.append(float(m["ts"]) * 1e9)
        elif m["ev"] == "drm_vblank_event":
            v = re.search(r"crtc=(\d+), seq=(\d+), time=(\d+)", m["body"])
            if v:
                vblanks[int(v[1])].append((int(v[3]), int(v[2])))
    if proc.wait():
        raise RuntimeError(f"trace-cmd report failed: {run}")
    return sorted(hypr), {k: sorted(v) for k, v in vblanks.items()}


def analyze(run):
    _, frames = burst_stats.load(f"{run}/burst.csv")
    hypr, outputs = events(run)
    # Feedback must agree with an actual traced vblank to within 1 ms.
    scores = {}
    for crtc, vs in outputs.items():
        pairs = collections.defaultdict(list)
        for t, seq in vs:
            pairs[seq].append(t)
        scores[crtc] = sum(any(abs(t - r["presented"]) <= 1e6
                               for t in pairs[r["msc"]])
                           for r in frames if r["state"] == "presented")
    if not scores or max(scores.values()) == 0:
        raise ValueError(f"no output matches presentation feedback: {run}")
    winners = [k for k, v in scores.items() if v == max(scores.values())]
    if len(winners) != 1:
        raise ValueError(f"ambiguous presentation output: {run}: {scores}")
    crtc = winners[0]
    vtimes = [t for t, _ in outputs[crtc]]
    out = []
    for r in frames:
        if r["state"] != "presented":
            continue
        cb, commit = r["t_intended"], r["t_present"]
        hi = bisect.bisect_right(hypr, commit)
        vi = bisect.bisect_right(vtimes, cb)
        if not 0 < hi < len(hypr) or not vi:
            continue
        row = dict(run=run, burst=r["burst"], frame=r["frame"], paced=r["paced"],
                   crtc=crtc, feedback_matches=scores[crtc], outputs=len(outputs),
                   callback_since_vblank=(cb - vtimes[vi - 1]) / 1e6,
                   callback_to_submit=(r["t_submit"] - cb) / 1e6,
                   present_call=(r["t_present_ret"] - commit) / 1e6,
                   commit_since_vblank=(commit - vtimes[bisect.bisect_right(vtimes, commit) - 1]) / 1e6,
                   prev_hypr_to_commit=(commit - hypr[hi - 1]) / 1e6,
                   commit_to_next_hypr=(hypr[hi] - commit) / 1e6,
                   next_hypr_to_present=(r["presented"] - hypr[hi]) / 1e6,
                   latency=(r["presented"] - cb) / 1e6,
                   commit_to_present=(r["presented"] - commit) / 1e6,
                   hypr_between=int(bisect.bisect_left(hypr, commit) >
                                    bisect.bisect_right(hypr, cb)))
        out.append(row)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--csv", help="optional per-frame output (captures remain untouched)")
    args = ap.parse_args()
    all_rows = []
    cols = ("callback_since_vblank", "callback_to_submit", "prev_hypr_to_commit",
            "commit_to_next_hypr", "next_hypr_to_present", "latency")
    print("run group n crtc/outputs matched " + " ".join(cols) + " hypr_between_pct")
    for run in args.runs:
        rows = analyze(run.rstrip("/"))
        all_rows.extend(rows)
        for label, rs in (("f0", [r for r in rows if r["frame"] == 0]),
                          ("f1+", [r for r in rows if r["frame"] > 0])):
            if not rs:
                continue
            med = lambda k: statistics.median(r[k] for r in rs)
            print(f"{run} {label} {len(rs)} {rs[0]['crtc']}/{rs[0]['outputs']} "
                  f"{rs[0]['feedback_matches']} " + " ".join(f"{med(k):.3f}" for k in cols)
                  + f" {100 * statistics.mean(r['hypr_between'] for r in rs):.1f}")
    if args.csv and all_rows:
        with open(args.csv, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(all_rows[0]))
            writer.writeheader()
            writer.writerows(all_rows)


if __name__ == "__main__":
    main()
