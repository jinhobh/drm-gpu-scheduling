"""Fair drm_sched policy with bounded sleeper credit (BORE/CFS-style interactivity).

* KERNEL and HIGH entities are strictly ahead; NORMAL/LOW share by virtual GPU time:
  vruntime += gpu_time << shift[prio]  (shifts KERNEL=1, HIGH=2, NORMAL=4, LOW=7).
* min_vr is a monotonic clock (cfs_rq->min_vruntime): the smallest vruntime among
  entities that have work queued or in flight.
* An entity waking from idle is placed at max(vr, min_vr - CREDIT << shift): it keeps
  up to CREDIT µs of GPU time it did not use while asleep (GENTLE_FAIR_SLEEPERS).
  Short-burst clients therefore jump ahead of hogs for a whole frame, while a hog
  that sleeps briefly between jobs only earns as much credit as it slept.
* HW queue depth: at most one NORMAL/LOW job on the ring at a time, so an urgent
  frame never waits behind two non-preemptible hog jobs.

Kernel mapping: vr/min_vr are drm_sched_entity / drm_sched_rq fields; idle detection
is the entity queue becoming empty with no job in flight; gpu_time comes from the
scheduled/finished fence timestamps.
"""

from gpusim.api import PRIO_NORMAL, PolicyBase

SHIFT = (1, 2, 4, 7)
CREDIT = 16000        # µs of unused GPU time an idle entity may bank


class Policy(PolicyBase):
    def __init__(self, sched):
        super().__init__(sched)
        self.min_vr = 0

    def entity_init(self, e):
        e.ps.vr = self.min_vr

    def job_push(self, e, j):
        if e.idle_since_us is not None:
            e.ps.vr = max(e.ps.vr, self.min_vr - (CREDIT << SHIFT[e.priority]))

    def job_done(self, e, j):
        e.ps.vr += j.gpu_us << SHIFT[e.priority]

    def select(self):
        s = self.sched
        active = [e for e in s.entities if e.queue or e.inflight]
        if active:
            self.min_vr = max(self.min_vr, min(e.ps.vr for e in active))
        normal_busy = False
        for j in s.hw_jobs:
            o = s.lookup(j.ring, j.entity_id)
            if o is not None and o.priority >= PRIO_NORMAL:
                normal_busy = True
        best = None
        best_key = None
        for e in active:
            if not e.ready or (normal_busy and e.priority >= PRIO_NORMAL):
                continue
            key = (min(e.priority, PRIO_NORMAL), e.ps.vr, e.id)
            if best_key is None or key < best_key:
                best, best_key = e, key
        return best
