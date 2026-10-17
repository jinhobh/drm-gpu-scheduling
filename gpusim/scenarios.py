"""Scenario suite.  FROZEN — the optimization agent must not edit this file.

Calibrated loosely to a Krackan Point APU (Radeon 860M, 8 RDNA3.5 CUs, LPDDR5X,
512 MiB VRAM carveout) driving a 120 Hz laptop panel or a 60 Hz external monitor.

Each scenario is run with several seeds.  The ``holdout`` split re-draws seeds AND
perturbs costs, burst rates, refresh rate and VRAM size; it exists to catch
policies overfit to the dev suite and is reserved for the human.
"""

import random

from .api import PRIO_LOW
from .workloads import Animator, Compositor, Compute, Game, Launcher, Mimic

DEV_SEEDS = (1, 2, 3, 4, 5)
HOLDOUT_SEEDS = (1001, 1002, 1003, 1004, 1005)

SCENARIOS = {}


def scenario(name, desc, checks=(), **cfg):
    def deco(fn):
        SCENARIOS[name] = dict(name=name, desc=desc, checks=frozenset(checks), cfg=cfg, build=fn)
        return fn
    return deco


class Knobs:
    """Holdout perturbation: cost scale `kc`, burst-rate scale `kr`."""

    def __init__(self, kc=1.0, kr=1.0):
        self.kc = kc
        self.kr = kr

    def c(self, us):
        return us * self.kc

    def r(self, hz):
        return hz * self.kr


# ---- reusable client presets ------------------------------------------------------
def widget(sim, rng, k, name, rate=0.35):
    return Animator(sim, rng, name, rate_hz=k.r(rate), dur_s=(0.3, 0.9), cost_us=k.c(800), sigma=0.3,
                    first_mult=(1.5, 3.0), cpu_us=1200, first_cpu_us=2500, jobs=1, bo_mb=16, nbos=2)


def scroller(sim, rng, k, name="browser_scroll", rate=0.3):
    return Animator(sim, rng, name, rate_hz=k.r(rate), dur_s=(0.6, 2.0), cost_us=k.c(1800), sigma=0.3,
                    first_mult=(1.3, 2.5), cpu_us=2000, first_cpu_us=3000, jobs=2, bo_mb=48, nbos=4)


def glass(sim, rng, k, name="glass_panel", rate=0.3):
    # Liquid-glass style interaction: the app redraws AND the compositor runs extra
    # backdrop blur / refraction passes for as long as the interaction lasts.
    return Animator(sim, rng, name, rate_hz=k.r(rate), dur_s=(0.5, 2.5), cost_us=k.c(1200), sigma=0.25,
                    first_mult=(1.3, 2.0), cpu_us=1500, first_cpu_us=2000, jobs=2, bo_mb=24, nbos=3,
                    effect_us=k.c(2200))


def compositor(sim, rng, k):
    return Compositor(sim, rng, base_us=k.c(700), per_surface_us=k.c(250))


def game(sim, rng, k, **kw):
    kw.setdefault("frame_us", 14000)
    kw["frame_us"] = k.c(kw["frame_us"])
    return Game(sim, rng, **kw)


# ---- the suite -----------------------------------------------------------------------
@scenario("desktop_bursts",
          "No background load: widgets, scrolling and glass on a 120 Hz desktop. "
          "Guards against policies that hurt the easy case (holding, bubbles).")
def _desktop(sim, rng, k):
    compositor(sim, rng, k)
    widget(sim, rng, k, "widget_clock")
    widget(sim, rng, k, "widget_weather")
    widget(sim, rng, k, "panel_menu")
    scroller(sim, rng, k)
    glass(sim, rng, k)


@scenario("game_widgets",
          "Uncapped windowed game (4 jobs/frame, ~14 ms/frame, 2 frames queued) while "
          "widgets animate on a 120 Hz panel.")
def _game_widgets(sim, rng, k):
    compositor(sim, rng, k)
    game(sim, rng, k, mode="uncapped", frame_us=14000, jobs=4, bo_mb=300, nbos=30)
    widget(sim, rng, k, "widget_clock", rate=0.5)
    widget(sim, rng, k, "widget_weather", rate=0.5)
    widget(sim, rng, k, "panel_menu", rate=0.5)


@scenario("game_glass",
          "Heavier uncapped game (5 jobs/frame, ~20 ms/frame) during liquid-glass "
          "interactions (heavy compositor blur) and scrolling. Compositor sets deadline "
          "hints on client fences.", client_deadline_hints=True)
def _game_glass(sim, rng, k):
    compositor(sim, rng, k)
    game(sim, rng, k, mode="uncapped", frame_us=20000, jobs=5, bo_mb=260, nbos=26)
    glass(sim, rng, k, rate=0.45)
    scroller(sim, rng, k, rate=0.3)


@scenario("compute_scroll",
          "Long non-preemptible compute dispatches (~25 ms, 3 deep: ML/video effects) while "
          "the user scrolls and widgets animate, 60 Hz external monitor.", refresh_hz=60)
def _compute_scroll(sim, rng, k):
    compositor(sim, rng, k)
    Compute(sim, rng, "ml_infer", job_us=k.c(25000), sigma=0.35, depth=3, bo_mb=150, nbos=6)
    scroller(sim, rng, k, rate=0.45)
    widget(sim, rng, k, "widget_clock", rate=0.4)
    widget(sim, rng, k, "panel_menu", rate=0.4)


@scenario("launch_pressure",
          "App launches (120-220 MB of fresh textures each, launch zoom animation) into a "
          "nearly full 512 MB carveout while a 60 fps capped game (330 MB) is running.")
def _launch_pressure(sim, rng, k):
    compositor(sim, rng, k)
    game(sim, rng, k, name="game60", mode="capped", frame_us=7500, jobs=3, cpu_us=2000,
         bo_mb=330, nbos=33, fps_div=2)
    Launcher(sim, rng, every_s=(3.0, 5.0), first_ms=(k.c(8000) / 1000, k.c(20000) / 1000),
             intro_us=k.c(3500), launch_effect_us=k.c(1000))
    widget(sim, rng, k, "widget_clock", rate=0.4)
    widget(sim, rng, k, "panel_menu", rate=0.4)


@scenario("launch_hog",
          "App launches under VRAM pressure while an uncapped game hogs the GPU.")
def _launch_hog(sim, rng, k):
    compositor(sim, rng, k)
    game(sim, rng, k, mode="uncapped", frame_us=13000, jobs=4, bo_mb=300, nbos=30)
    Launcher(sim, rng, every_s=(3.0, 5.0), first_ms=(k.c(8000) / 1000, k.c(20000) / 1000),
             intro_us=k.c(3500), launch_effect_us=k.c(1000))
    widget(sim, rng, k, "widget_clock", rate=0.4)


@scenario("fair_hogs",
          "Three equal-priority backlogged clients with very different job sizes (1.2 ms x8 "
          "deep, 12 ms game frames, 25 ms dispatches) plus widgets. GPU time must be shared "
          "fairly (Jain >= 0.95).", checks=("fair",))
def _fair_hogs(sim, rng, k):
    compositor(sim, rng, k)
    Compute(sim, rng, "small_jobs", job_us=k.c(1200), sigma=0.2, depth=8, gap_us=50, bo_mb=40, nbos=4)
    game(sim, rng, k, mode="uncapped", frame_us=12000, jobs=4, bo_mb=200, nbos=20)
    Compute(sim, rng, "big_jobs", job_us=k.c(25000), sigma=0.3, depth=2, bo_mb=100, nbos=4)
    widget(sim, rng, k, "widget_clock", rate=0.4)
    widget(sim, rng, k, "panel_menu", rate=0.4)


@scenario("mimic_attack",
          "A hog disguised as interactive (6 ms job, sleep ~1 ms, repeat) competes with a game. "
          "It must not get more than its fair half of the contended GPU (share <= 0.55).",
          checks=("mimic",))
def _mimic(sim, rng, k):
    compositor(sim, rng, k)
    Mimic(sim, rng, "mimic", job_us=k.c(6000), sleep_us=(500, 1500))
    game(sim, rng, k, mode="uncapped", frame_us=14000, jobs=4, bo_mb=250, nbos=25)
    widget(sim, rng, k, "widget_clock", rate=0.4)
    widget(sim, rng, k, "panel_menu", rate=0.4)
    scroller(sim, rng, k, rate=0.25)


@scenario("priority_mix",
          "LOW-priority batch compute + NORMAL uncapped game + widgets/glass. LOW must keep "
          "making progress (no starvation) but must not hurt interactivity.")
def _priority_mix(sim, rng, k):
    compositor(sim, rng, k)
    Compute(sim, rng, "batch_low", job_us=k.c(20000), sigma=0.3, depth=3, bo_mb=80, nbos=4, prio=PRIO_LOW)
    game(sim, rng, k, mode="uncapped", frame_us=12000, jobs=4, bo_mb=250, nbos=25)
    widget(sim, rng, k, "widget_clock", rate=0.4)
    glass(sim, rng, k, rate=0.3)


@scenario("video_mix",
          "60 fps video playback (steady, must not drop) + uncapped game + widgets and glass. "
          "Deadline hints on client fences.", client_deadline_hints=True)
def _video_mix(sim, rng, k):
    compositor(sim, rng, k)
    Game(sim, rng, "video", mode="capped", frame_us=k.c(1800), sigma=0.15, jobs=1, cpu_us=800,
         bo_mb=24, nbos=3, fps_div=2)
    game(sim, rng, k, mode="uncapped", frame_us=12000, jobs=4, bo_mb=220, nbos=22)
    widget(sim, rng, k, "widget_clock", rate=0.4)
    glass(sim, rng, k, rate=0.3)


def make(name, seed, split="dev"):
    """Return (cfg_overrides, knobs, build_fn, rng) for one run."""
    sc = SCENARIOS[name]
    cfg = dict(sc["cfg"])
    knobs = Knobs()
    if split == "holdout":
        pr = random.Random(f"holdout:{name}:{seed}")
        knobs = Knobs(kc=pr.uniform(0.8, 1.25), kr=pr.uniform(0.7, 1.4))
        base_hz = cfg.get("refresh_hz", 120)
        cfg["refresh_hz"] = pr.choice([base_hz, 60, 90, 144])
        cfg["vram_mb"] = pr.choice([512, 512, 1024])
        cfg["client_deadline_hints"] = pr.random() < 0.5
    rng = random.Random(f"{name}:{seed}")
    cfg["id_seed"] = f"ids:{name}:{seed}:{split}"
    return cfg, knobs, sc["build"], rng
