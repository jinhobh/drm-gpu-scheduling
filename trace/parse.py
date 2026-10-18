"""Per-job latency breakdown from a capture (step 1.2).

    python3 trace/parse.py traces/<name>/<timestamp> [--all]

Writes <run>/jobs.csv (one row per drm_sched job) and prints per-client percentiles.
Only jobs queued between the workload_start/end markers are summarised unless --all.

Every job's queue→done time splits into five parts (ms):

    dep_wait    queued, waiting for its own dependency fences
    order_wait  waiting for earlier jobs of the same entity to be scheduled (an entity's
                jobs run in order, so a blocked predecessor blocks it; no policy can help)
    sched_wait  ready (deps met, predecessors scheduled), not yet handed to the ring; of which
      credit_wait   the ring already had credit_limit (2) jobs in flight
                    (the rest is the scheduler choosing someone else, or work-item latency)
    ring_wait   handed to the ring, waiting behind earlier jobs (non-preemptible, in order)
    exec        running: HW start = max(run, previous job's done on the same ring)

Dependencies are the union of drm_sched_job_add_dep and drm_sched_job_unschedulable
(the latter also reports fences the driver adds in prepare_job, e.g. VMID waits).
Dependency readiness follows drm_sched: a dependency on another job's finished fence on the
same ring only waits until that job is *scheduled* (pipelining); on another ring until it
is done; scheduled fences (context = finished context - 1) until it is scheduled; any
other fence until its dma_fence_signaled event. A fence that signalled before the trace
began counts as ready at queue time.

amdgpu kernel jobs (VM updates, clears, moves) have negative client ids.
"""

import argparse
import bisect
import collections
import csv
import re
import statistics
import subprocess
import sys

CREDIT_LIMIT = 2   # amdgpu.sched_hw_submission
LINE = re.compile(r"^\s*(?P<task>.+?)-(?P<tid>\d+)\s+\[\d+\]\s+\S+\s+(?P<ts>\d+\.\d+):\s+"
                  r"(?P<ev>\w+):\s+(?P<body>.*)$")
FENCE = re.compile(r"fence=(\d+):(\d+)")
SIGNALED = re.compile(r"context=(\d+) seqno=(\d+)")


class Job:
    __slots__ = ("ctx", "seq", "ring", "client", "task", "tid", "pid", "comm", "prio",
                 "t_ioctl", "t_queue", "t_run", "t_done", "deps", "t_deps", "t_ready", "t_hw",
                 "credit")

    def __init__(self, ctx, seq):
        self.ctx, self.seq = ctx, seq
        self.ring = self.task = self.comm = ""
        self.client = self.tid = self.pid = self.prio = None
        self.t_ioctl = self.t_queue = self.t_run = self.t_done = None
        self.t_deps = self.t_ready = self.t_hw = None
        self.deps = []
        self.credit = 0.0


def read_trace(run):
    """Jobs keyed by finished fence (ctx, seq), signal times, workload window."""
    out = subprocess.run(["trace-cmd", "report", "-i", f"{run}/trace.dat"],
                         capture_output=True, text=True, check=True).stdout
    jobs, signaled, ioctl = {}, {}, {}
    window = [None, None]
    for line in out.splitlines():
        m = LINE.match(line)
        if not m:
            continue
        ev, body, t = m["ev"], m["body"], float(m["ts"])
        if ev == "dma_fence_signaled":
            s = SIGNALED.search(body)
            if s:
                signaled[(int(s[1]), int(s[2]))] = t
            continue
        if ev == "print":
            if "capture: workload_start" in body:
                window[0] = t
            elif "capture: workload_end" in body:
                window[1] = t
            continue
        f = FENCE.search(body)
        if not f:
            continue
        key = (int(f[1]), int(f[2]))
        if ev == "amdgpu_cs_ioctl":
            ioctl[key] = t
            continue
        j = jobs.get(key)
        if ev == "drm_sched_job_queue":
            j = jobs[key] = Job(*key)
            j.t_queue, j.task, j.tid = t, m["task"], int(m["tid"])
            j.ring = re.search(r"ring=([^,]+)", body)[1]
            c = int(re.search(r"client_id:(\d+)", body)[1])
            j.client = c - (1 << 64) if c >= 1 << 63 else c
            j.t_ioctl = ioctl.pop(key, None)
        elif j is None:
            continue
        elif ev == "drm_sched_job_run":
            j.t_run = t
        elif ev == "drm_sched_job_done":
            j.t_done = t
        elif ev in ("drm_sched_job_add_dep", "drm_sched_job_unschedulable"):
            d = FENCE.findall(body)[1]
            d = (int(d[0]), int(d[1]))
            if d not in j.deps:
                j.deps.append(d)
    return jobs, signaled, window


def read_bpf(run, jobs):
    """pid, comm and priority of the submitter, keyed by the job's finished fence."""
    try:
        lines = open(f"{run}/bpf.txt")
    except OSError:
        return
    for line in lines:
        if not line.startswith("push "):
            continue
        f = line.rstrip("\n").split(" ", 8)
        if len(f) < 9:
            continue
        ctx, seq = f[3].split(":")
        j = jobs.get((int(ctx), int(seq)))
        if j:
            j.pid, j.prio, j.comm = int(f[2]), int(f[4]), f[8]


def resolve_deps(jobs, signaled):
    sched_ctx = {}   # scheduled-fence context -> finished context
    for (ctx, _) in jobs:
        sched_ctx[ctx - 1] = ctx
    for j in jobs.values():
        ready = j.t_queue
        for d in j.deps:
            k = jobs.get(d)
            if k is not None:
                t = k.t_run if k.ring == j.ring else k.t_done
            elif d[0] in sched_ctx and (sched_ctx[d[0]], d[1]) in jobs:
                t = jobs[(sched_ctx[d[0]], d[1])].t_run
            else:
                t = signaled.get(d)
            if t is not None and t > ready:
                ready = t
        j.t_deps = ready
        prev = jobs.get((j.ctx, j.seq - 1))
        j.t_ready = max(ready, prev.t_run) if prev is not None else ready


def ring_timing(jobs):
    """HW start per job, and time each job spent ready while its ring's credits were full."""
    by_ring = collections.defaultdict(list)
    for j in jobs.values():
        if j.t_run is not None and j.t_done is not None:
            by_ring[j.ring].append(j)
    for ring_jobs in by_ring.values():
        ring_jobs.sort(key=lambda j: j.t_run)
        prev_done = None
        for j in ring_jobs:
            j.t_hw = j.t_run if prev_done is None else max(j.t_run, prev_done)
            prev_done = j.t_done if prev_done is None else max(prev_done, j.t_done)

        # intervals with >= CREDIT_LIMIT jobs between run and done, as sorted disjoint spans
        edges = sorted([(j.t_run, 1) for j in ring_jobs] + [(j.t_done, -1) for j in ring_jobs])
        starts, ends, n, open_t = [], [], 0, None
        for t, d in edges:
            n += d
            if n >= CREDIT_LIMIT and open_t is None:
                open_t = t
            elif n < CREDIT_LIMIT and open_t is not None:
                starts.append(open_t)
                ends.append(t)
                open_t = None
        cum = [0.0]
        for s, e in zip(starts, ends):
            cum.append(cum[-1] + e - s)

        def full_before(t):
            i = bisect.bisect_right(starts, t)
            if i == 0:
                return 0.0
            return cum[i - 1] + min(t, ends[i - 1]) - starts[i - 1]

        for j in ring_jobs:
            lo = min(j.t_ready, j.t_run)
            j.credit = full_before(j.t_run) - full_before(lo)


def ms(x):
    return round(x * 1e3, 4)


def write_csv(run, jobs):
    cols = ["ctx", "seq", "ring", "client", "pid", "comm", "prio", "t_queue", "deps",
            "ioctl_to_queue", "dep_wait", "order_wait", "sched_wait", "credit_wait", "ring_wait",
            "exec", "total"]
    with open(f"{run}/jobs.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for j in sorted(jobs.values(), key=lambda j: j.t_queue):
            if j.t_hw is None:
                continue
            p = parts(j)
            w.writerow([j.ctx, j.seq, j.ring, j.client, j.pid, j.comm or j.task, j.prio,
                        f"{j.t_queue:.6f}", len(j.deps),
                        ms(j.t_queue - j.t_ioctl) if j.t_ioctl else "",
                        *(ms(p[k]) for k in KEYS)])


KEYS = ("dep_wait", "order_wait", "sched_wait", "credit_wait", "ring_wait", "exec", "total")


def parts(j):
    deps = min(j.t_deps, j.t_run)
    ready = min(j.t_ready, j.t_run)
    return {"dep_wait": deps - j.t_queue, "order_wait": ready - deps,
            "sched_wait": j.t_run - ready, "credit_wait": j.credit,
            "ring_wait": j.t_hw - j.t_run, "exec": j.t_done - j.t_hw, "total": j.t_done - j.t_queue}


def pct(xs, q):
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def summarise(jobs, window, all_jobs):
    groups = collections.defaultdict(list)
    for j in jobs.values():
        if j.t_hw is None:
            continue
        if not all_jobs and window[0] is not None and not (
                window[0] <= j.t_queue <= (window[1] or float("inf"))):
            continue
        name = (j.comm or j.task).split(":")[0][:14]
        client = j.client if j.client >= 0 else f"k{j.client}"
        groups[(name, client, j.prio if j.prio is not None else "?", j.ring)].append(parts(j))

    keys = KEYS
    print(f"{'submitter':14} {'client':>6} {'pr':>2} {'ring':11} {'jobs':>6}  "
          + "  ".join(f"{k:>17}" for k in keys))
    print(f"{'':44}" + "  ".join(f"{'p50/p99/max ms':>17}" for _ in keys))
    for g, rows in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if len(rows) < 5:
            continue
        cells = []
        for k in keys:
            xs = sorted(r[k] * 1e3 for r in rows)
            cells.append(f"{statistics.median(xs):5.2f}/{pct(xs, .99):5.2f}/{xs[-1]:5.1f}")
        print(f"{g[0]:14} {g[1]!s:>6} {g[2]!s:>2} {g[3]:11} {len(rows):>6}  "
              + "  ".join(f"{c:>17}" for c in cells))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--all", action="store_true", help="summarise jobs outside the workload window")
    args = ap.parse_args()
    run = args.run.rstrip("/")
    jobs, signaled, window = read_trace(run)
    read_bpf(run, jobs)
    jobs = {k: j for k, j in jobs.items() if j.t_run is not None and j.t_done is not None}
    resolve_deps(jobs, signaled)
    ring_timing(jobs)
    write_csv(run, jobs)
    print(f"{len(jobs)} complete jobs -> {run}/jobs.csv", file=sys.stderr)
    summarise(jobs, window, args.all)


if __name__ == "__main__":
    main()
