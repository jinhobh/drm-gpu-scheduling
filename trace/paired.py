#!/usr/bin/env python3
"""Paired-block t intervals and pilot precision planning; no frame bootstrap.

Input CSV: stage,block,metric,value; one paired difference per block/metric.
Values must already have the prespecified direction/transform (e.g. log ratio).
Use only pilot rows for sample-size planning; they never become confirmatory data.
"""
import argparse
import csv
import itertools
import math
import statistics


def t_cdf(t, df):
    """Student t CDF for positive integer df, using a cosine-integral recurrence."""
    if df < 1 or not isinstance(df, int):
        raise ValueError('positive integer degrees of freedom required')
    if t < 0:
        return 1 - t_cdf(-t, df)
    theta = math.atan(t / math.sqrt(df))
    sine, cosine = math.sin(theta), math.cos(theta)
    power = df - 1
    integral = theta if power % 2 == 0 else sine
    for n in range(2 + power % 2, power + 1, 2):
        integral = sine * cosine ** (n - 1) / n + (n - 1) / n * integral
    scale = math.exp(math.lgamma((df + 1) / 2) - math.lgamma(df / 2)) / math.sqrt(math.pi)
    return min(1.0, 0.5 + scale * integral)


def t95(df):
    lo, hi = 0.0, 1.0
    while t_cdf(hi, df) < 0.975:
        hi *= 2
    for _ in range(70):
        mid = (lo + hi) / 2
        if t_cdf(mid, df) < 0.975:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def interval(values):
    if len(values) < 2 or not all(math.isfinite(x) for x in values):
        raise ValueError('at least two finite independent paired-block values required')
    mean, sd = statistics.mean(values), statistics.stdev(values)
    half = t95(len(values) - 1) * sd / math.sqrt(len(values))
    return mean, sd, mean - half, mean + half


def sign_flip(values):
    """Two-sided exact sensitivity test; requires null sign symmetry/exchangeability."""
    if not 1 <= len(values) <= 20 or not all(math.isfinite(x) for x in values):
        raise ValueError('exact sign flip supports 1..20 finite block values')
    observed = abs(sum(values))
    count = sum(abs(sum(s * x for s, x in zip(signs, values))) >= observed - 1e-12
                for signs in itertools.product((-1, 1), repeat=len(values)))
    return count / 2 ** len(values)


def plan_blocks(values, half_width, minimum=8, maximum=60, inflation=1.5):
    _, sd, _, _ = interval(values)
    if sd == 0:
        raise ValueError('zero pilot variance cannot determine the block count; expand pilot')
    if not (math.isfinite(half_width) and half_width > 0 and
            math.isfinite(inflation) and inflation >= 1 and 2 <= minimum <= maximum <= 1000):
        raise ValueError('invalid precision-planning settings')
    for n in range(minimum, maximum + 1):
        if t95(n - 1) * sd * inflation / math.sqrt(n) <= half_width:
            return n
    return None  # Precision infeasible within the declared planning resource bound.


def load(stream):
    data = {}
    stages = set()
    for row in csv.DictReader(stream):
        stage, block, metric = row['stage'], row['block'], row['metric']
        if stage not in ('pilot', 'confirmatory') or not block or not metric:
            raise ValueError('invalid stage/block/metric')
        stages.add(stage)
        key = (block, metric)
        if key in data:
            raise ValueError(f'duplicate block/metric: {key}; frames are not blocks')
        data[key] = float(row['value'])
    if len(stages) != 1:
        raise ValueError('empty input or mixed pilot/confirmatory rows')
    by_metric = {}
    for (block, metric), value in data.items():
        by_metric.setdefault(metric, {})[block] = value
    sets = [set(v) for v in by_metric.values()]
    if any(s != sets[0] for s in sets):
        raise ValueError('metrics have different/missing blocks; resolve missing-data rules first')
    return stages.pop(), by_metric


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv')
    parser.add_argument('--target', action='append', default=[], metavar='METRIC=HALF_WIDTH')
    parser.add_argument('--min-blocks', type=int, default=8)
    parser.add_argument('--max-blocks', type=int, default=60)
    parser.add_argument('--sd-inflation', type=float, default=1.5)
    args = parser.parse_args()
    try:
        targets = {name: float(value) for name, value in (s.split('=', 1) for s in args.target)}
        with open(args.csv) as stream:
            stage, metrics = load(stream)
        if targets and stage != 'pilot':
            raise ValueError('block-count planning accepts pilot data only')
        if set(targets) - metrics.keys():
            raise ValueError('target names do not match input metrics')
        planned = []
        for name, blocks in sorted(metrics.items()):
            values = [blocks[b] for b in sorted(blocks)]
            mean, sd, lo, hi = interval(values)
            p = sign_flip(values) if len(values) <= 20 else 'not-enumerated'
            print(f'{name}: n={len(values)} mean={mean:.6g} sd={sd:.6g} t95=[{lo:.6g},{hi:.6g}] sign_flip_p={p}')
            if name in targets:
                n = plan_blocks(values, targets[name], args.min_blocks, args.max_blocks, args.sd_inflation)
                planned.append(n)
                print(f'  pilot precision blocks: {n if n else "infeasible within resource bound"}')
        if targets:
            result = max(planned) if all(n is not None for n in planned) else 'infeasible'
            print(f'Proposed common block count: {result}; freeze separately before confirmatory data')
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(1, f'{exc}\n')


if __name__ == '__main__':
    main()
