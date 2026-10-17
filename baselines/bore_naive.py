"""Strawman: a direct, untuned port of BORE's core idea to drm_sched.  Calibration only —
it shows the environment rewards the idea; the agent is expected to do much better.

* burst  = GPU time an entity consumed since it last went idle (BORE: CPU time since
  last sleep).  Reset when the entity idles.
* penalty = max(0, log2(burst) - offset) * scale, smoothed (rises slowly, falls fast),
  turned into extra vruntime weight like a nice-level shift.
* fair core = the upstream-style vruntime queue from baselines/fair.py, but with the
  weight shift taken from priority + burst penalty, KERNEL/HIGH kept strictly first.
* when a low-burst ("interactive") entity was active recently, hogs get one HW slot.
"""

import math

from gpusim.api import PRIO_HIGH, PolicyBase

OFFSET = 12.0      # log2(µs): bursts below ~4 ms are free
SCALE = 1.0        # penalty levels per doubling beyond the offset
MAX_PEN = 12.0
RECENT_US = 50_000


class Policy(PolicyBase):
    def __init__(self, sched):
        super().__init__(sched)
        self.last_interactive = -10**9

    def entity_init(self, e):
        e.ps.vr = None
        e.ps.burst = 0.0
        e.ps.pen = 0.0

    def _cur_pen(self, e):
        b = e.ps.burst
        p = max(0.0, (math.log2(b + 1.0) - OFFSET) * SCALE) if b > 0 else 0.0
        return min(MAX_PEN, max(p, e.ps.pen))

    def _min_vr(self):
        vs = [x.ps.vr for x in self.sched.entities if x.ps.vr is not None and (x.queue or x.inflight)]
        return min(vs) if vs else 0.0

    def job_push(self, e, j):
        if e.ps.vr is None or (e.idle_since_us is not None):
            e.ps.vr = max(e.ps.vr or 0.0, self._min_vr())

    def job_done(self, e, j):
        e.ps.burst += j.gpu_us
        pen = self._cur_pen(e)
        e.ps.vr += j.gpu_us * (2.0 ** (e.priority * 2 + pen))
        if pen < 1.0:
            self.last_interactive = self.sched.now

    def entity_idle(self, e):
        new = self._cur_pen(e)
        # binary smoothing: rise by half the difference, fall immediately
        e.ps.pen = e.ps.pen + (new - e.ps.pen) / 2 if new > e.ps.pen else new
        e.ps.burst = 0.0

    def select(self):
        best = None
        best_key = None
        hog_slots = 1 if self.sched.now - self.last_interactive < RECENT_US else self.sched.credit_limit
        hogs_inflight = sum(1 for j in self.sched.hw_jobs
                            if self._pen_of(j.entity_id) >= 1.0)
        for e in self.sched.entities:
            if not e.ready:
                continue
            pen = self._cur_pen(e)
            if pen >= 1.0 and e.priority > PRIO_HIGH and hogs_inflight >= hog_slots:
                continue
            strict = 0 if e.priority <= PRIO_HIGH else 1
            key = (strict, e.priority if strict == 0 else 0, e.ps.vr, e.id)
            if best_key is None or key < best_key:
                best, best_key = e, key
        return best

    def _pen_of(self, eid):
        for e in self.sched.entities:
            if e.id == eid:
                return self._cur_pen(e)
        return 0.0
