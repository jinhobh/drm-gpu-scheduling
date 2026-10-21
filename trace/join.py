#!/usr/bin/env python3
"""Per-frame latency breakdown: burst_client's frame log joined with the kernel trace (1.2b).

    python3 trace/join.py RUN [RUN...]          # RUN = traces/<name>/<timestamp>
    python3 trace/join.py RUN --log hog.csv     # join another burst_client log in RUN
    python3 trace/join.py RUN --trace-only      # no burst.csv: frames from the trace alone

Writes RUN/frames.csv and prints, per run, medians by frame-in-burst (frame 0 vs the rest).
With several runs it also prints one line per run, so a matrix can be compared at a glance.

Each frame is matched to the gfx job(s) its vkQueueSubmit pushed: same tid, and
drm_sched_job_queue between t_submit and t_submit_ret. Its start latency then splits into
(ms, all CLOCK_MONOTONIC):

    submit       t_submit -> drm_sched_job_queue (CPU: ioctl, BO validation)
    dep          waiting for its own dependency fences
    order        behind an earlier job of its own entity
    pick         ready, credits free, scheduler ran someone else first (sched_wait - credit)
    credit       ready, but the ring already had 2 jobs in flight
    ring         handed to the ring, behind earlier non-preemptible jobs (t_hw - t_run)
    wake         gpu_start - t_hw: HW start to the first timestamp in the command buffer
                 (preamble, VM flush, GFXOFF exit; small if the t_hw model is right)

and `busy_by`: which clients' jobs executed on the ring while the frame was ready but not
yet running ([t_ready, t_hw]), with their share in ms. `ok` checks the timestamp mapping:
gpu_start must lie inside [t_run, t_done] (within TOL_US).

--trace-only (also used when RUN has no burst.csv) takes the victim's frames from the trace:
the gfx jobs of the burst_client process that started last (the vk-hog is a burst_client
too, started earlier), split into bursts at gaps > BURST_GAP_S, keeping bursts whose job
count is a multiple of the frames per burst (`-n`, 8 for --calib) and not PROBE_JOBS (the
timestamp probes at start and exit). Then start_lat = t_hw - t_queue, exec_gpu = the trace's exec (which
includes wake), and submit, wake and ok are unknown. Idle gaps shorter than BURST_GAP_S merge
bursts; they are split again by count, so idle_ms is only right for gaps > BURST_GAP_S.
"""

import argparse
import bisect
import collections
import csv
import os
import re
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import burst_stats  # noqa: E402
import parse  # noqa: E402

TOL_US = 100        # the ts correction is good to ~±75 us
SUBMIT_SLACK = 50e-6
FALLBACK_S = 2e-3
BURST_GAP_S = 0.1
PROBE_JOBS = 50     # burst_client's timestamp probes at start and exit
LOOSE_US = 100      # a correction looser than this is replaced by the fence alignment
TAIL_REF_MS = 0.04  # fence signal after the last timestamp: median 0.03-0.06 ms in m2


def load_jobs(run):
    jobs, signaled, window = parse.read_trace(run)
    parse.read_bpf(run, jobs)
    jobs = {k: j for k, j in jobs.items() if j.t_run is not None and j.t_done is not None}
    parse.resolve_deps(jobs, signaled)
    parse.ring_timing(jobs)
    return jobs


def client_name(j):
    name = (j.comm or j.task).split(":")[0][:14]
    return name if j.client is None or j.client >= 0 else f"kernel{j.client}"


class Ring:
    """Jobs of one ring sorted by HW start, for 'who was running while I waited'."""

    def __init__(self, jobs):
        self.jobs = sorted(jobs, key=lambda j: j.t_hw)
        self.starts = [j.t_hw for j in self.jobs]
        # jobs are short (< 100 ms), so scanning back that far finds every overlap
        self.max_len = max((j.t_done - j.t_hw for j in self.jobs), default=0)

    def busy_by(self, lo, hi, me):
        out = collections.Counter()
        if hi <= lo:
            return out
        i = bisect.bisect_left(self.starts, lo - self.max_len)
        while i < len(self.jobs) and self.jobs[i].t_hw < hi:
            j = self.jobs[i]
            i += 1
            if j is me:
                continue
            ov = min(j.t_done, hi) - max(j.t_hw, lo)
            if ov > 0:
                out[client_name(j)] += ov
        return out


def match(rows, jobs, pid):
    """{row index: [jobs]} for gfx jobs queued by the row's thread during its vkQueueSubmit.

    If the thread queued none (a driver submit thread), fall back to any thread of the
    process (bpf push pid) up to FALLBACK_S after vkQueueSubmit returned."""
    by_tid, by_pid = collections.defaultdict(list), []
    for j in jobs.values():
        if j.ring.startswith("gfx"):
            by_tid[j.tid].append(j)
            if j.pid == pid:
                by_pid.append(j)
    for js in [*by_tid.values(), by_pid]:
        js.sort(key=lambda j: j.t_queue)
    pid_keys = [j.t_queue for j in by_pid]
    out = {}
    for n, r in enumerate(rows):
        js = by_tid.get(r["tid"], [])
        keys = [j.t_queue for j in js]
        lo = r["t_submit"] / 1e9 - SUBMIT_SLACK
        hi = r["t_submit_ret"] / 1e9 + SUBMIT_SLACK
        found = js[bisect.bisect_left(keys, lo):bisect.bisect_right(keys, hi)]
        if not found:
            found = by_pid[bisect.bisect_left(pid_keys, lo):
                           bisect.bisect_right(pid_keys, hi + FALLBACK_S)][:1]
        if found:
            out[n] = found
    return out


def make_rings(jobs):
    rings = collections.defaultdict(list)
    for j in jobs.values():
        if j.t_hw is not None:
            rings[j.ring].append(j)
    return {k: Ring(v) for k, v in rings.items()}


def describe(o, j, rings):
    """Add job j's wait breakdown and ring occupancy to frame row o."""
    p = parse.parts(j)
    o.update(ctx=j.ctx, seq=j.seq, ring=j.ring, prio=j.prio,
             dep=p["dep_wait"] * 1e3, order=p["order_wait"] * 1e3,
             pick=(p["sched_wait"] - p["credit_wait"]) * 1e3, credit=p["credit_wait"] * 1e3,
             ring_wait=p["ring_wait"] * 1e3, exec_trace=p["exec"] * 1e3)
    busy = rings[j.ring].busy_by(min(j.t_ready, j.t_run), j.t_hw, j)
    o["busy_ms"] = sum(busy.values()) * 1e3
    o["busy_by"] = " ".join(f"{c}={t * 1e3:.2f}" for c, t in busy.most_common(3))


def corr_err_us(header):
    """burst_client's timestamp-correction error (us) from the `# clock` header line."""
    for h in header:
        m = re.search(r"ts_corr_ns=-?\d+\+-(\d+)", h)
        if m:
            return int(m.group(1)) / 1e3
    return float("nan")


def realign(rows, matched):
    """Shift GPU times (ns, in place) so the median tail is TAIL_REF_MS; return the shift (ms).

    The fence signals right after the frame's last timestamp, also under load, while the
    loose-probe correction can be off by up to a hog frame."""
    tails = [js[-1].t_done * 1e3 - rows[n]["gpu_end"] / 1e6 for n, js in matched.items()]
    if not tails:
        return 0.0
    shift = statistics.median(tails) - TAIL_REF_MS
    for r in rows:
        r["gpu_start"] += round(shift * 1e6)
        r["gpu_end"] += round(shift * 1e6)
    return shift


def frame_rows(rows, jobs, pid=None, align=False):
    """Frame rows; with align, re-anchor the GPU times on the fences first (see realign)."""
    rings = make_rings(jobs)
    matched = match(rows, jobs, pid)
    shift = realign(rows, matched) if align else 0.0
    out = []
    for n, r in enumerate(rows):
        js = matched.get(n)
        o = {k: r[k] for k in ("burst", "frame", "idle_ms", "state")}
        o["n_jobs"] = len(js) if js else 0
        o["start_lat"] = (r["gpu_start"] - r["t_submit"]) / 1e6
        o["exec_gpu"] = r["exec_us"] / 1e3
        if not js:
            out.append(o)
            continue
        j = js[-1]  # the job that carries the frame's command buffer is the last one
        describe(o, j, rings)
        gs, ge = r["gpu_start"] / 1e9, r["gpu_end"] / 1e9
        tol = TOL_US * 1e-6
        o.update(submit=(j.t_queue - r["t_submit"] / 1e9) * 1e3, wake=(gs - j.t_hw) * 1e3,
                 tail=(j.t_done - ge) * 1e3,
                 ok=int(j.t_run - tol <= gs <= j.t_done + tol and ge <= j.t_done + tol))
        out.append(o)
    return out, shift


def frames_per_burst(run):
    w = meta(run).get("workload", "")
    if "--calib" in w:
        return 8
    m = re.search(r"(?:-n|--frames)\s+(\d+)", w)
    return int(m[1]) if m else 30


def trace_frame_rows(jobs, n_frames):
    """Frame rows from the victim's jobs alone (see --trace-only)."""
    first = {}
    for j in jobs.values():
        if (j.comm or j.task).startswith("burst_client") and j.ring.startswith("gfx"):
            first[j.pid] = min(first.get(j.pid, j.t_queue), j.t_queue)
    if not first:
        return []
    victim = max(first, key=first.get)
    js = sorted((j for j in jobs.values() if j.pid == victim and j.ring.startswith("gfx")),
                key=lambda j: j.t_queue)
    bursts, prev = [], None
    for j in js:
        if prev is None or j.t_queue - prev.t_queue > BURST_GAP_S:
            bursts.append((0 if prev is None else j.t_queue - prev.t_queue, []))
        bursts[-1][1].append(j)
        prev = j
    rings = make_rings(jobs)
    out, b = [], 0
    for gap, bj in bursts:
        if len(bj) % n_frames or len(bj) == PROBE_JOBS:
            continue
        for k in range(0, len(bj), n_frames):
            for f, j in enumerate(bj[k:k + n_frames]):
                o = {"burst": b, "frame": f, "idle_ms": round(gap * 1e3) if k == 0 else "",
                     "state": "", "n_jobs": 1, "start_lat": (j.t_hw - j.t_queue) * 1e3}
                describe(o, j, rings)
                o["exec_gpu"] = o["exec_trace"]
                out.append(o)
            b += 1
    return out


COLS = ("burst", "frame", "idle_ms", "state", "n_jobs", "ctx", "seq", "ring", "prio",
        "start_lat", "submit", "dep", "order", "pick", "credit", "ring_wait", "wake",
        "exec_gpu", "exec_trace", "tail", "ok", "busy_ms", "busy_by")


def write(path, frames):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, COLS, extrasaction="ignore")
        w.writeheader()
        for o in frames:
            w.writerow({k: round(v, 4) if isinstance(v, float) else v for k, v in o.items()})


def med(xs):
    return statistics.median(xs) if xs else float("nan")


SHOW = ("start_lat", "submit", "dep", "order", "pick", "credit", "ring_wait", "wake", "exec_gpu",
        "busy_ms")


def group_stats(frames):
    m = [o for o in frames if o["n_jobs"]]
    s = {k: med([o[k] for o in m if k in o]) for k in SHOW}
    s["n"], s["matched"] = len(frames), len(m)
    ok = [o["ok"] for o in m if "ok" in o]
    s["ok"] = sum(ok) / len(ok) if ok else float("nan")
    busy = collections.Counter()
    for o in m:
        for kv in o["busy_by"].split():
            c, t = kv.rsplit("=", 1)
            busy[c] += float(t)
    s["top"] = " ".join(f"{c}={t / max(len(m), 1):.2f}" for c, t in busy.most_common(2))
    return s


def print_table(title, groups):
    print(title)
    print(f"{'':10} {'n':>4} {'match':>5} {'ok':>4}  "
          + " ".join(f"{k[:9]:>9}" for k in SHOW) + "  busy_by (mean ms/frame)")
    for name, s in groups:
        print(f"{name:10} {s['n']:4d} {s['matched']:5d} {s['ok']:4.2f}  "
              + " ".join(f"{s[k]:9.3f}" for k in SHOW) + f"  {s['top']}")


def meta(run):
    m = {}
    try:
        for line in open(os.path.join(run, "meta.txt")):
            k, _, v = line.partition(":")
            m[k.strip()] = v.strip()
    except OSError:
        pass
    return m


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--log", default="burst.csv", help="burst_client log inside RUN (burst.csv)")
    ap.add_argument("--trace-only", action="store_true", help="ignore the log, use the trace")
    ap.add_argument("-q", "--quiet", action="store_true", help="only the per-run/condition tables")
    a = ap.parse_args()
    summary = []
    by_cond = collections.defaultdict(list)
    for run in a.runs:
        run = run.rstrip("/")
        log = os.path.join(run, a.log)
        if a.trace_only or not os.path.exists(log):
            frames = trace_frame_rows(load_jobs(run), frames_per_burst(run))
            out = os.path.join(run, "frames-trace.csv")
        else:
            header, rows = burst_stats.load(log)
            pid = next((int(w[4:]) for h in header for w in h.split() if w.startswith("pid=")),
                       None)
            err = corr_err_us(header)
            frames, shift = frame_rows(rows, load_jobs(run), pid, align=not err <= LOOSE_US)
            if shift:
                print(f"{run}: timestamp correction +-{err:.0f} us, re-aligned on fences "
                      f"by {shift:+.3f} ms (wake/tail/ok are then relative to TAIL_REF_MS)",
                      file=sys.stderr)
            out = os.path.join(run, "frames" + ("" if a.log == "burst.csv" else "-" + a.log))
            out = out if out.endswith(".csv") else out + ".csv"
        write(out, frames)
        f0 = [o for o in frames if o["frame"] == 0]
        f1 = [o for o in frames if o["frame"] > 0]
        if not a.quiet:
            print_table(f"\n{out}  (dpm {meta(run).get('dpm_level', '?')}, "
                        f"policy {meta(run).get('gpu_sched.sched_policy', '?')}; medians, ms)",
                        [("frame 0", group_stats(f0)), ("frames 1+", group_stats(f1))])
        summary.append((run, group_stats(f1)))
        cond = re.sub(r"-r\d+$", "", os.path.basename(os.path.dirname(run)))
        by_cond[cond].append(frames)
    if len(by_cond) > 1 and len(by_cond) < len(summary):
        groups = []
        for cond in sorted(by_cond):
            fs = [o for frames in by_cond[cond] for o in frames]
            groups.append((f"{cond} f0", group_stats([o for o in fs if o["frame"] == 0])))
            groups.append((f"{cond} f1+", group_stats([o for o in fs if o["frame"] > 0])))
        width = max(len(g) for g, _ in groups)
        print(f"\nper condition, repeats pooled (medians, ms)")
        print(f"{'':{width}} {'n':>4} {'match':>5} {'ok':>4}  "
              + " ".join(f"{k[:9]:>9}" for k in SHOW) + "  busy_by (mean ms/frame)")
        for g, s in groups:
            print(f"{g:{width}} {s['n']:4d} {s['matched']:5d} {s['ok']:4.2f}  "
                  + " ".join(f"{s[k]:9.3f}" for k in SHOW) + f"  {s['top']}")
    if len(summary) > 1:
        width = max(len(r) for r, _ in summary)
        print(f"\nframes 1+ per run (medians, ms)\n{'run':{width}} {'start':>7} {'pick':>7} "
              f"{'credit':>7} {'ring':>7} {'wake':>7} {'exec':>7} {'ok':>5}")
        for r, s in summary:
            print(f"{r:{width}} {s['start_lat']:7.3f} {s['pick']:7.3f} {s['credit']:7.3f} "
                  f"{s['ring_wait']:7.3f} {s['wake']:7.3f} {s['exec_gpu']:7.3f} {s['ok']:5.2f}")


if __name__ == "__main__":
    main()
