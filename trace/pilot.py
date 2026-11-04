#!/usr/bin/env python3
"""Prepare (never execute) a six-boot FIFO precision pilot, outside confirmation.

Also classify a CSV produced by pacing.py --csv FILE with the `pacing` subcommand.
"""
import argparse
import csv
import importlib.util
import math
from pathlib import Path
import random
import shlex

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('kernel_arms', REPO / 'trace/kernel/arms.py')
arms = importlib.util.module_from_spec(spec)
spec.loader.exec_module(arms)


def plan(seed, blocks=3):
    if blocks < 2:
        raise ValueError('at least two pilot blocks required')
    rng = random.Random(seed)
    orders = [i % 2 for i in range(blocks)]
    rng.shuffle(orders)
    rows = []
    for block, reverse in enumerate(orders, 1):
        names = ['fifo/off/stock', 'fifo/bg1/stock']
        if reverse:
            names.reverse()
        for session, arm in enumerate(names, 1):
            mode = arms.load()[arm]['admission']
            name = f'fifo-pilot-b{block:02d}-a{mode}'
            capture = shlex.quote(str(REPO / 'trace/capture.sh'))
            burst = shlex.quote(str(REPO / 'trace/workloads/burst.sh'))
            probe = '--iters 736 -n 10 -i 300 -b 4'
            commands = '\n'.join([
                f'sudo {shlex.quote(str(REPO / "trace/dpm.sh"))} auto',
                f'sudo {capture} {name}-pre -- {burst} none {probe}',
                f'sudo DPMS=auto REPS=3 HOGS="none vk-hog vk-hog1 vkmark-cube" '
                f'{shlex.quote(str(REPO / "trace/matrix.sh"))} {name}',
                f'sudo {capture} {name}-post -- {burst} none {probe}',
            ])
            rows.append(dict(stage='pilot', seed=seed, block=block, session=session,
                             arm=arm, cmdline=arms.cmdline(arm), matrix=name,
                             mechanism_reference=arms.mechanism_reference(arm), commands=commands))
    return rows


def pacing_mode(rows, refresh_ms):
    if not math.isfinite(refresh_ms) or refresh_ms <= 0:
        raise ValueError('positive finite measured refresh period required')
    values = [float(r['latency']) / refresh_ms for r in rows
              if int(r['frame']) > 0 and int(r['paced']) == 1 and math.isfinite(float(r['latency']))]
    if len(values) < 10:
        return 'unknown'
    for mode in (2, 3):
        if sum(abs(v - mode) <= 0.25 for v in values) / len(values) >= 0.8:
            return f'{mode}-refresh'
    return 'mixed'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    p = subs.add_parser('plan')
    p.add_argument('--seed', type=int, required=True)
    p.add_argument('--blocks', type=int, default=3)
    p.add_argument('--out', type=Path, required=True)
    p = subs.add_parser('pacing')
    p.add_argument('csv', type=Path)
    p.add_argument('--refresh-ms', type=float, required=True)
    args = parser.parse_args()
    if args.command == 'plan':
        rows = plan(args.seed, args.blocks)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open('x', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        print(f'{len(rows)} boot/login sessions written to {args.out}; no commands executed')
    else:
        with args.csv.open() as stream:
            print(pacing_mode(list(csv.DictReader(stream)), args.refresh_ms))


if __name__ == '__main__':
    main()
