"""Run one (policy, scenario, seed) simulation.  FROZEN."""

import time

from . import metrics, scenarios
from .engine import Sim
from .loader import load_policy


def run(policy_path, scenario, seed, split="dev", trusted=False, keep_sim=False, trace=False):
    cls = load_policy(policy_path, trusted=trusted)
    cfg, knobs, build, rng = scenarios.make(scenario, seed, split)
    cfg["trace"] = trace
    t0 = time.perf_counter()
    sim = Sim(cfg, cls)
    build(sim, rng, knobs)
    sim.run()
    m = metrics.collect(sim)
    m["wall_s"] = time.perf_counter() - t0
    m["scenario"] = scenario
    m["seed"] = seed
    if keep_sim:
        return m, sim
    return m
