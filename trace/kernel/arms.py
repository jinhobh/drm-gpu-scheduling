#!/usr/bin/env python3
"""Single arm registry: emit boot arguments or a readable table, never boot."""
import argparse
import csv
from pathlib import Path


def load():
    with Path(__file__).with_name('arms.csv').open() as stream:
        rows = list(csv.DictReader(stream))
    arms = {r['arm']: r for r in rows}
    if len(rows) != len(arms):
        raise ValueError('duplicate arm')
    for row in rows:
        for key, valid in [('policy', {'1', '2'}), ('admission', {'0', '1', '2'}),
                           ('selection', {'0', '1'})]:
            if row[key] not in valid:
                raise ValueError(f'invalid {key}')
        if row['contrast_control'] and row['contrast_control'] not in arms:
            raise ValueError('unknown contrast control')
    return arms


def cmdline(arm):
    row = load()[arm]
    return (f"gpu_sched.sched_policy={row['policy']} "
            f"amdgpu.sched_gfx_admission={row['admission']} "
            f"amdgpu.sched_gfx_selection={row['selection']} amdgpu.user_queue=0")


def mechanism_reference(arm):
    policy = load()[arm]['policy']
    return next(r['arm'] for r in load().values()
                if r['policy'] == policy and r['admission'] == '0' and r['selection'] == '0')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('arm', nargs='?', choices=load())
    args = parser.parse_args()
    if args.arm:
        print(cmdline(args.arm))
    else:
        for name in load():
            print(name, cmdline(name), sep='\t')
