"""Approximation of upstream's CFS-inspired "fair" drm_sched policy (Tvrtko Ursulin,
"Fair(er) DRM scheduler", merged for 7.2 then reverted as default).

* One run queue for all priorities, ordered by virtual GPU time.
* vruntime += gpu_time << shift[prio]; shifts KERNEL=1, HIGH=2, NORMAL=4, LOW=7
  (each step down gets ~2x/4x/8x less GPU time).  Accounted when a job finishes.
* An entity leaves the tree when its queue empties and remembers its lag relative to the
  tree minimum; on re-entry it is placed at min_vruntime + lag.
* "Favour interactive clients slightly": an entity re-joining with zero lag goes just in
  front of the head if its average job is shorter than the scheduler average (EWMA),
  just behind otherwise; a lower-priority one goes further back.
"""

from gpusim.api import PolicyBase

SHIFT = (1, 2, 4, 7)


def ewma(avg, x):
    # DECLARE_EWMA(drm_sched_avgtime, 6, 4): weight 1/16
    return x if avg == 0 else avg + (x - avg) / 16.0


class Policy(PolicyBase):
    def __init__(self, sched):
        super().__init__(sched)
        self.sched_avg = 0.0

    def entity_init(self, e):
        e.ps.vr = 0.0
        e.ps.lag = 0.0
        e.ps.in_tree = False
        e.ps.avg = 0.0

    def _tree(self, skip=None):
        return [e for e in self.sched.entities if e.ps.in_tree and e is not skip]

    def _min_vr(self, skip=None):
        t = self._tree(skip)
        return min(e.ps.vr for e in t) if t else None

    def job_push(self, e, j):
        if e.ps.in_tree:
            return
        head = None
        for x in self._tree():
            if head is None or (x.ps.vr, x.id) < (head.ps.vr, head.id):
                head = x
        if head is None:
            e.ps.vr = max(e.ps.vr, 0.0)
        elif e.ps.lag > 0:
            e.ps.vr = head.ps.vr + e.ps.lag
        elif e.priority > head.priority:
            e.ps.vr = head.ps.vr + ((1 + e.ps.avg + self.sched_avg) * (1 << SHIFT[e.priority]))
        elif e.priority < head.priority:
            e.ps.vr = head.ps.vr - (head.priority - e.priority) * 1e-3
        else:
            e.ps.vr = head.ps.vr + (-1e-3 if e.ps.avg <= self.sched_avg else 1e-3)
        e.ps.lag = 0.0
        e.ps.in_tree = True

    def job_run(self, e, j):
        if not e.queue:
            m = self._min_vr(skip=e)
            e.ps.lag = max(0.0, e.ps.vr - m) if m is not None else 0.0
            e.ps.in_tree = False

    def job_done(self, e, j):
        dv = j.gpu_us * (1 << SHIFT[e.priority])
        if e.ps.in_tree:
            e.ps.vr += dv
        else:
            e.ps.lag += dv
            e.ps.vr += dv
        e.ps.avg = ewma(e.ps.avg, j.gpu_us)
        self.sched_avg = ewma(self.sched_avg, j.gpu_us)

    def select(self):
        best = None
        for e in self.sched.entities:
            if e.ready and e.ps.in_tree:
                if best is None or (e.ps.vr, e.id) < (best.ps.vr, best.id):
                    best = e
        return best
