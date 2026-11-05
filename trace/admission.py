#!/usr/bin/env python3
"""Validate bg1 reservation traces from a RUN directory or trace-cmd report.

Checks transitions in their per-scheduler serial order, not cross-CPU report
order. Capture boundaries may cut live reservations; --require-drained rejects
those. This validates observed bg1 accounting, not physical/direct submissions,
selection fairness, hardware recovery, or global1 admission.
"""
import argparse
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
import re
import subprocess


EVENT = re.compile(r"\bdrm_sched_admission(_select|_dispatch)?:\s+(.*)")
FIELD = re.compile(r"(\w+)=([^\s,]+)")


def validate(lines, require_drained=False):
    transitions = defaultdict(list)
    scans = defaultdict(list)
    dispatches = defaultdict(list)
    for line in lines:
        if re.search(r"\bLOST\b.*\bEVENTS?\b", line, re.I):
            raise ValueError("trace reports lost events; recapture")
        match = EVENT.search(line)
        if not match:
            continue
        raw = dict(FIELD.findall(match[2]))
        try:
            key = (raw.pop("dev"), raw.pop("ring"))
            row = {k: v if k == "fence" else int(v) for k, v in raw.items()}
            if match[1] == "_select":
                if not (0 <= row["skipped"] <= row["examined"]):
                    raise ValueError("invalid scan counts")
                if row["duration_ns"] < 0 or row["reason"] not in (0, -2, -11, -28):
                    raise ValueError("invalid selection result")
                if (row["reason"] == 0) != (row["priority"] in range(4)):
                    raise ValueError("selection priority/result mismatch")
                scans[key].append(row)
            elif match[1] == "_dispatch":
                for field in ("reservation", "bg_limit", "priority", "credits", "limit", "fence"):
                    row[field]
                if row["bg_limit"] == 1:
                    dispatches[key].append(row)
            else:
                for field in ("serial", "action", "priority", "count", "credits", "limit", "fence"):
                    row[field]
                transitions[key].append(row)
        except (KeyError, ValueError) as exc:
            raise ValueError(f"malformed admission event: {line.strip()}") from exc
    if not transitions:
        raise ValueError("no bg1 reservation events; cannot validate this capture")
    if not any(r["priority"] in (2, 3) for rows in dispatches.values() for r in rows):
        raise ValueError("no bg1 background dispatch events; cannot cross-check reservations")
    for key, rows in dispatches.items():
        if key not in transitions and any(
                r["priority"] not in (0, 1) or r["reservation"] or
                not 0 <= r["credits"] <= r["limit"] or r["limit"] < 1 for r in rows):
            raise ValueError(f"{key}: dispatch without reservation history")

    results = []
    for key, rows in sorted(transitions.items()):
        rows.sort(key=lambda r: r["serial"])
        first = rows[0]
        initial = first["count"] - (1 if first["action"] == 0 else -1)
        if initial not in (0, 1):
            raise ValueError(f"{key}: invalid initial occupancy {initial}")
        unknown = initial
        count = initial
        active = set()
        seen = set()
        acquisitions = {}
        boundary_job = None
        previous = first["serial"] - 1
        for row in rows:
            serial, fence = row["serial"], row["fence"]
            if serial <= 0 or serial != previous + 1:
                raise ValueError(f"{key}: missing/duplicate transition near serial {serial}")
            previous = serial
            if row["priority"] not in (2, 3):
                raise ValueError(f"{key}: HIGH/KERNEL/invalid priority reserved at {serial}")
            if not 0 <= row["credits"] <= row["limit"] or row["limit"] < 1:
                raise ValueError(f"{key}: original credit bound violated at {serial}")
            if row["action"] == 0:
                if fence in active or fence in seen:
                    raise ValueError(f"{key}: duplicate reservation {fence}")
                seen.add(fence)
                active.add(fence)
                acquisitions[serial] = row
                count += 1
            elif row["action"] in (1, 2):
                if fence in active:
                    active.remove(fence)
                elif unknown and fence not in seen:
                    unknown -= 1  # Reservation began before capture.
                    seen.add(fence)
                    boundary_job = (fence, row["priority"])
                else:
                    raise ValueError(f"{key}: release without reservation {fence}")
                count -= 1
            else:
                raise ValueError(f"{key}: invalid action at {serial}")
            if count not in (0, 1) or row["count"] != count:
                raise ValueError(f"{key}: cap/count violated at {serial}")
        if require_drained and (initial or count):
            raise ValueError(f"{key}: capture has open boundary reservations")
        dispatched = set()
        for row in dispatches[key]:
            if row["fence"] in dispatched:
                raise ValueError(f"{key}: duplicate dispatch {row['fence']}")
            dispatched.add(row["fence"])
            if (row["priority"] not in range(4) or row["limit"] < 1 or
                    not 0 <= row["credits"] <= row["limit"]):
                raise ValueError(f"{key}: invalid dispatch priority/credits")
            reservation = row["reservation"]
            if row["priority"] in (0, 1):
                if reservation:
                    raise ValueError(f"{key}: exempt dispatch has reservation")
            elif reservation in acquisitions:
                acquire = acquisitions[reservation]
                if (acquire["fence"], acquire["priority"]) != (row["fence"], row["priority"]):
                    raise ValueError(f"{key}: dispatch/reservation identity mismatch")
            elif initial and 0 < reservation < first["serial"]:
                identity = (row["fence"], row["priority"])
                if boundary_job is not None and boundary_job != identity:
                    raise ValueError(f"{key}: inconsistent boundary reservation")
                boundary_job = identity
            else:
                raise ValueError(f"{key}: background dispatch without observed reservation")
        selections = scans[key]
        results.append(dict(dev=key[0], ring=key[1], transitions=len(rows),
                            initial=initial, final=count,
                            dispatches=len(dispatches[key]),
                            selections=len(selections),
                            skipped=sum(r["skipped"] for r in selections),
                            max_examined=max((r["examined"] for r in selections), default=0),
                            max_select_ns=max((r["duration_ns"] for r in selections), default=0)))
    return results


@contextmanager
def report(path):
    path = Path(path)
    if path.is_dir():
        with subprocess.Popen(["trace-cmd", "report", "-i", str(path / "trace.dat")],
                              stdout=subprocess.PIPE, text=True) as proc:
            try:
                yield proc.stdout
            finally:
                # Drain after an early validation failure so the child cannot
                # block on a full pipe while its context waits for exit.
                for _ in proc.stdout:
                    pass
            if proc.wait():
                raise ValueError("trace-cmd report failed")
    else:
        with path.open() as stream:
            yield stream


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_or_report", nargs="+")
    parser.add_argument("--require-drained", action="store_true")
    args = parser.parse_args()
    for path in args.run_or_report:
        try:
            with report(path) as lines:
                results = validate(lines, args.require_drained)
        except (OSError, ValueError) as exc:
            parser.exit(1, f"{path}: INVALID: {exc}\n")
        for row in results:
            print(path, "observed bg1 transitions OK", " ".join(f"{k}={v}" for k, v in row.items()))


if __name__ == "__main__":
    main()
