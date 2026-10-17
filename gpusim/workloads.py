"""Client (workload) models.  FROZEN — the optimization agent must not edit this file.

Every random draw is keyed by a policy-independent index (vblank number, frame number,
burst number) on a per-client stream, so two policies always face the *same* demands:
frame N of the game costs the same no matter when it gets to run.

Interactive frame accounting (hidden from policies, see metrics.py):
  * each frame opportunity gets a FrameRec with the vblank it should hit in an
    unloaded system (``Job._ideal``) — misses caused by the client's own cost are
    not charged, only misses caused by contention.
"""

import math
import random

from .api import PRIO_HIGH, PRIO_NORMAL
from .engine import Client


class Draws:
    """Indexed random stream: draw k is the same regardless of call order."""

    def __init__(self, seed):
        self._r = random.Random(seed)
        self._c = []

    def u(self, k, i=0):
        c = self._c
        r = self._r
        while len(c) <= k:
            c.append((r.random(), r.random(), r.random(), r.random(), r.random(), r.random()))
        return c[k][i]

    def n(self, k, i=0):
        u1 = self.u(k, i) or 1e-12
        u2 = self.u(k, i + 1)
        return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)

    def ln(self, k, median, sigma, i=0):
        return median * math.exp(sigma * self.n(k, i))


def _hint_latch(sim, now):
    d = sim.display
    return d.latch_idx(now) * d.period - d.lead


# --------------------------------------------------------------------------- compositor
class Compositor(Client):
    """Wayland compositor (HIGH priority).  Wakes `lead` before each vblank while anything
    animates, latches finished client buffers, renders the composite, which must finish
    by vblank.  Its fence carries a deadline = vblank (drm atomic sets plane fence deadlines).
    """

    def __init__(self, sim, rng, *, base_us=700.0, per_surface_us=250.0, cpu_us=300,
                 jitter=0.08, scanout_mb=20, tex_mb=48):
        super().__init__(sim, "compositor", "comp")
        self.draw = Draws(rng.random())
        self.base = base_us
        self.per_surface = per_surface_us
        self.cpu_us = cpu_us
        self.jitter = jitter
        self.ent = self.new_entity("gfx", PRIO_HIGH)
        mem = sim.mem
        self.scanout = [mem.alloc(self, scanout_mb, pinned=True) for _ in range(3)]
        self.tex = [mem.alloc(self, tex_mb / 4.0) for _ in range(4)]
        self.pending = None
        self.k = 0
        d = sim.display
        sim.at(d.period - d.lead, self._wake, d.period)

    def _wake(self, V):
        sim = self.sim
        d = sim.display
        sim.at(V + d.period - d.lead, self._wake, V + d.period)
        act, surf, eff = d.active(V)
        if not act:
            return
        rec = sim.frame(self, "comp", 2, "vblank", V, "comp")
        if self.pending is not None and self.pending.done_us is None:
            rec.skipped = True
            return
        vi = V // d.period
        cost = (self.base + surf * self.per_surface + eff) * (1.0 + self.jitter * (2 * self.draw.u(vi) - 1))
        sim.after(self.cpu_us, self._submit, cost, rec, V)

    def _submit(self, cost, rec, V):
        self.k += 1
        bos = [self.scanout[self.k % 3]] + self.tex
        self.pending = self.sim.submit(self.ent, cost, bos=bos, frame=rec, deadline_us=V, tag="comp")


# --------------------------------------------------------------------------- bursts
class Animator(Client):
    """Spontaneous animation bursts: widgets, scrolling, liquid-glass interactions.

    Input at t0 -> first frame after `first_cpu_us` (heavier: layout, glyph/atlas
    uploads) -> one frame per vblank until the burst ends.  At most 2 frames in
    flight; a tick that finds 2 in flight is a dropped frame.  `effect_us` is extra
    compositor work while the burst is active (backdrop blur / refraction passes).
    """

    def __init__(self, sim, rng, name, *, rate_hz, dur_s=(0.3, 1.0), cost_us=900.0, sigma=0.25,
                 first_mult=(1.5, 3.0), cpu_us=1500, first_cpu_us=2500, jobs=1, bo_mb=24, nbos=3,
                 effect_us=0.0, surfaces=1, start_s=0.4, prio=PRIO_NORMAL):
        super().__init__(sim, name, "interactive")
        self.draw = Draws(rng.random())
        self.first_draw = Draws(rng.random())
        self.cost_us = cost_us
        self.sigma = sigma
        self.first_mult = first_mult
        self.cpu_us = cpu_us
        self.first_cpu_us = first_cpu_us
        self.jobs = jobs
        self.ent = self.new_entity("gfx", prio)
        self.bos = [sim.mem.alloc(self, bo_mb / nbos) for _ in range(nbos)]
        self.inflight = 0
        self.bursts = []
        t = start_s * 1e6 + rng.expovariate(rate_hz) * 1e6
        while t < sim.end - 0.3e6:
            d = rng.uniform(*dur_s) * 1e6
            t1 = t + d
            sim.display.add_window(t, t1 + first_cpu_us, surfaces, effect_us)
            self.bursts.append((int(t), int(t1)))
            sim.at(t, self._burst, len(self.bursts) - 1, int(t), int(t1))
            t = t1 + rng.expovariate(rate_hz) * 1e6

    def _burst(self, bi, t0, t1):
        sim = self.sim
        rec = sim.frame(self, "first", 4, "latch", t0, "resp")
        fd = self.first_draw
        mult = self.first_mult[0] + (self.first_mult[1] - self.first_mult[0]) * fd.u(bi, 2)
        cost = fd.ln(bi, self.cost_us, self.sigma) * mult
        sim.after(self.first_cpu_us, self._submit, cost, rec)
        V = sim.display.next_vblank(t0 + self.first_cpu_us + 1)
        sim.at(V, self._tick, V, t1)

    def _tick(self, V, t1):
        if V >= t1:
            return
        sim = self.sim
        P = sim.display.period
        sim.at(V + P, self._tick, V + P, t1)
        rec = sim.frame(self, "frame", 1, "latch", V, "burst")
        if self.inflight >= 2:
            rec.skipped = True
            return
        vi = V // P
        cpu = self.cpu_us * (0.8 + 0.4 * self.draw.u(vi, 2))
        sim.after(int(cpu), self._submit, self.draw.ln(vi, self.cost_us, self.sigma), rec)

    def _submit(self, cost, rec):
        sim = self.sim
        self.inflight += 1
        part = cost / self.jobs
        j = None
        for i in range(self.jobs):
            last = i == self.jobs - 1
            j = sim.submit(self.ent, part, bos=self.bos, frame=rec if last else None, tag="frame")
        if sim.cfg["client_deadline_hints"]:
            sim.set_deadline(j, _hint_latch(sim, sim.now))
        sim.on_done(j, self._done)

    def _done(self):
        self.inflight -= 1


# --------------------------------------------------------------------------- games / video
class Game(Client):
    """3D app.  mode='uncapped': renders as fast as possible with `max_inflight` frames
    queued (throughput class).  mode='capped': one frame every `fps_div` vblanks
    (interactive 'steady' class — its frames count as jank too).  Video playback is a
    capped Game with one small job per frame.
    """

    def __init__(self, sim, rng, name="game", *, mode="uncapped", frame_us=14000.0, sigma=0.12,
                 jobs=4, cpu_us=3000, max_inflight=2, bo_mb=300, nbos=30, fps_div=2,
                 prio=PRIO_NORMAL, start_s=0.0, stop_s=None):
        super().__init__(sim, name, "throughput" if mode == "uncapped" else "interactive")
        assert 1 <= jobs <= 6
        self.draw = Draws(rng.random())
        self.split = Draws(rng.random())
        self.mode = mode
        self.frame_us = frame_us
        self.sigma = sigma
        self.jobs = jobs
        self.cpu_us = cpu_us
        self.max_inflight = max_inflight
        self.fps_div = fps_div
        self.ent = self.new_entity("gfx", prio)
        self.bos = [sim.mem.alloc(self, bo_mb / nbos) for _ in range(nbos)]
        self.inflight = 0
        self.waiting = False
        self.n = 0
        start = int(start_s * 1e6)
        self.stop = sim.end if stop_s is None else int(stop_s * 1e6)
        sim.display.add_window(start, self.stop, 1, 0.0)
        if mode == "uncapped":
            sim.at(start, self._cpu)
        else:
            P = sim.display.period * fps_div
            first = sim.display.next_vblank(start)
            sim.at(first, self._tick, first)

    # uncapped
    def _cpu(self):
        if self.sim.now >= self.stop:
            return
        n = self.n
        cpu = self.cpu_us * (0.85 + 0.3 * self.draw.u(n, 5))
        self.sim.after(int(cpu), self._submit, n, None)
        self.n += 1

    # capped
    def _tick(self, V):
        sim = self.sim
        if V >= self.stop:
            return
        P = sim.display.period
        sim.at(V + P * self.fps_div, self._tick, V + P * self.fps_div)
        rec = sim.frame(self, "frame", 1, "latch", V, "steady")
        if self.inflight >= self.max_inflight:
            rec.skipped = True
            return
        n = V // P
        cpu = self.cpu_us * (0.85 + 0.3 * self.draw.u(n, 5))
        sim.after(int(cpu), self._submit, n, rec)

    def _submit(self, n, rec):
        sim = self.sim
        d = self.draw
        cost = d.ln(n, self.frame_us, self.sigma)
        ws = [0.5 + self.split.u(n, i) for i in range(self.jobs)]
        tot = sum(ws)
        self.inflight += 1
        rt = self.bos[:2]
        j = None
        for i in range(self.jobs):
            last = i == self.jobs - 1
            bos = rt + self.bos[2 + i::self.jobs]
            j = sim.submit(self.ent, cost * ws[i] / tot, bos=bos, frame=rec if last else None, tag="game")
        if rec is not None and sim.cfg["client_deadline_hints"]:
            sim.set_deadline(j, _hint_latch(sim, sim.now))
        sim.on_done(j, self._frame_done)
        if self.mode == "uncapped":
            if self.inflight < self.max_inflight:
                self._cpu()
            else:
                self.waiting = True

    def _frame_done(self):
        self.inflight -= 1
        if self.waiting:
            self.waiting = False
            self._cpu()


# --------------------------------------------------------------------------- compute / bulk
class Compute(Client):
    """Back-to-back GPU jobs kept `depth` deep (ML inference, video effects, a
    many-small-jobs hog when job_us is small).  Throughput class."""

    def __init__(self, sim, rng, name="compute", *, job_us=25000.0, sigma=0.35, depth=3, gap_us=300,
                 bo_mb=150, nbos=6, prio=PRIO_NORMAL, start_s=0.0):
        super().__init__(sim, name, "throughput")
        self.draw = Draws(rng.random())
        self.job_us = job_us
        self.sigma = sigma
        self.gap_us = gap_us
        self.ent = self.new_entity("gfx", prio)
        self.bos = [sim.mem.alloc(self, bo_mb / nbos) for _ in range(nbos)]
        self.n = 0
        for _ in range(depth):
            sim.at(int(start_s * 1e6), self._submit)

    def _submit(self):
        sim = self.sim
        cost = self.draw.ln(self.n, self.job_us, self.sigma)
        self.n += 1
        j = sim.submit(self.ent, cost, bos=self.bos, tag="bulk")
        sim.on_done(j, self._done)

    def _done(self):
        self.sim.after(self.gap_us, self._submit)


class Mimic(Client):
    """A throughput hog that *looks* interactive: one job, wait for it, sleep briefly,
    repeat.  Used to check that burst/sleep heuristics cannot be gamed."""

    def __init__(self, sim, rng, name="mimic", *, job_us=6000.0, sigma=0.2, sleep_us=(500, 1500),
                 bo_mb=60, nbos=4, prio=PRIO_NORMAL, start_s=0.0):
        super().__init__(sim, name, "throughput")
        self.mimic = True
        self.draw = Draws(rng.random())
        self.job_us = job_us
        self.sigma = sigma
        self.sleep_us = sleep_us
        self.ent = self.new_entity("gfx", prio)
        self.bos = [sim.mem.alloc(self, bo_mb / nbos) for _ in range(nbos)]
        self.n = 0
        sim.at(int(start_s * 1e6), self._submit)

    def _submit(self):
        sim = self.sim
        j = sim.submit(self.ent, self.draw.ln(self.n, self.job_us, self.sigma), bos=self.bos, tag="bulk")
        sim.on_done(j, self._done)

    def _done(self):
        a, b = self.sleep_us
        s = a + (b - a) * self.draw.u(self.n, 3)
        self.n += 1
        self.sim.after(int(s), self._submit)


# --------------------------------------------------------------------------- app launches
class Launcher:
    """Spawns fresh App clients (new drm_file, new entities, no history) every few seconds."""

    def __init__(self, sim, rng, *, every_s=(3.0, 5.0), start_s=1.5, total_mb=(120, 220),
                 init_ms=(60, 140), first_ms=(8, 20), intro_s=(0.4, 0.9), intro_us=3500.0,
                 life_s=(2.5, 4.0), launch_effect_us=1000.0, launch_anim_s=0.35, cpu_us=1500):
        t = start_s + rng.uniform(0, 0.5)
        n = 0
        while t < sim.end / 1e6 - 1.5:
            p = dict(
                seed=rng.random(),
                total_mb=rng.uniform(*total_mb),
                init_us=int(rng.uniform(*init_ms) * 1000),
                first_us=rng.uniform(*first_ms) * 1000,
                intro_us_len=int(rng.uniform(*intro_s) * 1e6),
                intro_cost=intro_us,
                life_us=int(rng.uniform(*life_s) * 1e6),
                cpu_us=cpu_us,
            )
            t0 = int(t * 1e6)
            sim.display.add_window(t0, t0 + int(launch_anim_s * 1e6), 0, launch_effect_us)
            a0 = t0 + p["init_us"]
            sim.display.add_window(a0, a0 + p["intro_us_len"], 1, 0.0)
            sim.at(t0, App, sim, n, t0, p)
            n += 1
            t += rng.uniform(*every_s)


class App(Client):
    def __init__(self, sim, n, t0, p):
        super().__init__(sim, f"app{n}", "interactive")
        self.draw = Draws(p["seed"])
        self.p = p
        self.t0 = t0
        self.gfx = self.new_entity("gfx", PRIO_NORMAL)
        self.sdma = self.new_entity("sdma", PRIO_NORMAL)
        self.bos = []
        self.uploads = []
        self.inflight = 0
        sim.after(15000, self._alloc)
        sim.at(t0 + p["init_us"], self._first)
        sim.at(t0 + p["life_us"], self._exit)

    def _alloc(self):
        sim = self.sim
        left = self.p["total_mb"]
        while left > 0:
            sz = min(16.0, left)
            left -= sz
            bo = sim.mem.alloc(self, sz)
            self.bos.append(bo)
            up = sim.submit(self.sdma, sz / sim.mem.bw, bos=[bo], tag="upload")
            self.uploads.append(up._fin)

    def _first(self):
        sim = self.sim
        rec = sim.frame(self, "first", 4, "latch", self.t0, "resp")
        self.inflight += 1
        jobs = 3
        cost = self.p["first_us"]
        j = None
        for i in range(jobs):
            last = i == jobs - 1
            j = sim.submit(self.gfx, cost / jobs, bos=self.bos[i::jobs] or self.bos,
                           deps=self.uploads if i == 0 else (), frame=rec if last else None, tag="first")
        if sim.cfg["client_deadline_hints"]:
            sim.set_deadline(j, _hint_latch(sim, sim.now))
        sim.on_done(j, self._done)
        V = sim.display.next_vblank(sim.now + 1)
        sim.at(V, self._tick, V, sim.now + self.p["intro_us_len"])

    def _tick(self, V, t1):
        if V >= t1:
            return
        sim = self.sim
        P = sim.display.period
        sim.at(V + P, self._tick, V + P, t1)
        rec = sim.frame(self, "frame", 1, "latch", V, "burst")
        if self.inflight >= 2:
            rec.skipped = True
            return
        vi = V // P
        cpu = self.p["cpu_us"] * (0.8 + 0.4 * self.draw.u(vi, 2))
        sim.after(int(cpu), self._submit, self.draw.ln(vi, self.p["intro_cost"], 0.25), rec)

    def _submit(self, cost, rec):
        sim = self.sim
        self.inflight += 1
        j = sim.submit(self.gfx, cost, bos=self.bos[:4], frame=rec, tag="frame")
        if sim.cfg["client_deadline_hints"]:
            sim.set_deadline(j, _hint_latch(sim, sim.now))
        sim.on_done(j, self._done)

    def _done(self):
        self.inflight -= 1

    def _exit(self):
        sim = self.sim
        for bo in self.bos:
            sim.mem.free_bo(bo)
        sim.fini_entity(self.gfx)
        sim.fini_entity(self.sdma)
