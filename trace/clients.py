"""Snapshot amdgpu DRM clients from /proc/*/fdinfo (run as root to see every process).

One JSON line per open amdgpu file: CLOCK_MONOTONIC time, pid, comm, drm-client-id, and
the fdinfo counters (engine busy ns, memory, doorbells). drm-client-id matches the
client_id field of the gpu_scheduler tracepoints. A non-zero doorbell count hints that the
client created user queues, which bypass drm_sched.

    python3 trace/clients.py              # one snapshot
    python3 trace/clients.py --every 0.5  # until interrupted
"""

import argparse
import json
import os
import sys
import time


def snapshot():
    t = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
    rows = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        fdinfo = f"/proc/{pid}/fdinfo"
        try:
            comm = open(f"/proc/{pid}/comm").read().strip()
            fds = os.listdir(fdinfo)
        except OSError:
            continue
        for fd in fds:
            try:
                text = open(f"{fdinfo}/{fd}").read()
            except OSError:
                continue
            if "drm-driver:\tamdgpu" not in text:
                continue
            row = {"t_ns": t, "pid": int(pid), "comm": comm, "fd": int(fd)}
            for line in text.splitlines():
                key, _, val = line.partition(":")
                if key.startswith(("drm-", "amd-")):
                    row[key] = val.strip()
            rows.append(row)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--every", type=float, default=0.0, help="seconds between snapshots")
    args = ap.parse_args()
    try:
        while True:
            for row in snapshot():
                print(json.dumps(row))
            sys.stdout.flush()
            if not args.every:
                break
            time.sleep(args.every)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
