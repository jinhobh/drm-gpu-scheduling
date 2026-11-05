"""Step 0.2/0.3 report: which GPU clients go through drm_sched, at what priority, with deadlines.

    python3 trace/gonogo.py traces/gonogo/<timestamp>

* GPU-active clients come from fdinfo engine counters (clients.jsonl). A client that uses
  the GPU but never appears in a gpu_scheduler event is submitting some other way (user
  queues / MES), invisible to drm_sched.
* Priorities and deadlines come from bpf.txt (trace/bpf/ids.bt).
"""

import collections
import json
import re
import subprocess
import sys

PRIO = {0: "KERNEL", 1: "HIGH", 2: "NORMAL", 3: "LOW"}
EVENT = re.compile(r"^\s*(?P<task>.+?)-(?P<pid>\d+)\s+\[\d+\]\s+(?:\S+\s+)?(?P<ts>\d+\.\d+):\s+"
                   r"(?P<event>\w+):\s+(?P<body>.*)$")


def sched_events(trace_dat):
    out = subprocess.run(["trace-cmd", "report", "-i", trace_dat], capture_output=True,
                         text=True, check=True).stdout
    for line in out.splitlines():
        m = EVENT.match(line)
        if m and m["event"].startswith("drm_sched_job"):
            yield m["event"], m["body"]


def main(run):
    # fdinfo: per (pid, client id), GPU busy time per engine over the capture
    first, last, comm = {}, {}, {}
    for line in open(f"{run}/clients.jsonl"):
        r = json.loads(line)
        key = (r["pid"], r.get("drm-client-id"))
        comm[key] = r["comm"]
        eng = {k[len("drm-engine-"):]: int(v.split()[0]) for k, v in r.items()
               if k.startswith("drm-engine-") and not k.startswith("drm-engine-capacity")}
        first.setdefault(key, eng)
        last[key] = (eng, r.get("drm-total-doorbell", "0"))

    # gpu_scheduler events per client id and ring
    jobs = collections.Counter()
    rings = collections.defaultdict(set)
    for event, body in sched_events(f"{run}/trace.dat"):
        if event != "drm_sched_job_queue":
            continue
        cid = re.search(r"client_id:(\d+)", body)
        ring = re.search(r"ring=([^,]+)", body)
        if cid:
            jobs[cid[1]] += 1
            rings[cid[1]].add(ring[1] if ring else "?")

    # bpf: priority per submitting process, deadlines per setter
    prio = collections.defaultdict(collections.Counter)
    sdl = collections.Counter()
    dl = collections.Counter()
    for line in open(f"{run}/bpf.txt"):
        f = line.rstrip("\n").split(" ", 8 if line.startswith("push ") else 5)
        if f[0] == "push" and len(f) == 9:
            prio[f[6]][(f[8], PRIO.get(int(f[4]), f[4]), PRIO.get(int(f[5]), f[5]))] += 1
        elif f[0] == "sdl" and len(f) == 6:
            sdl[f[5]] += 1
        elif f[0] == "dl" and len(f) == 6:
            dl[f[5]] += 1

    print(f"{'pid':>7} {'comm':<16} {'client':>7} {'gpu busy ms':>11} {'sched jobs':>10} "
          f"{'doorbell':>9}  rings / priority")
    for key in sorted(last, key=lambda k: comm[k]):
        pid, cid = key
        eng, bell = last[key]
        busy = sum(eng.get(k, 0) - first[key].get(k, 0) for k in eng) / 1e6
        n = jobs.get(cid, 0)
        if busy <= 0 and n == 0:
            continue
        flag = "  <-- busy but not in drm_sched" if busy > 1 and n == 0 else ""
        pr = ", ".join(f"{c} {p}/{rq}" for (c, p, rq), _ in prio.get(cid, {}).most_common(3))
        print(f"{pid:>7} {comm[key]:<16} {cid or '-':>7} {busy:>11.1f} {n:>10} {bell:>9}  "
              f"{','.join(sorted(rings.get(cid, []))) or '-'} {pr}{flag}")

    unknown = set(jobs) - {cid for _, cid in last}
    if unknown:
        print("\nclient ids in drm_sched but not in fdinfo (kernel jobs or exited clients):",
              {c: jobs[c] for c in sorted(unknown)})
    print("\ndrm_sched fence deadlines set by:", dict(sdl) or "none")
    print("dma_fence_set_deadline callers:", dict(dl) or "none")


if __name__ == "__main__":
    main(sys.argv[1].rstrip("/"))
