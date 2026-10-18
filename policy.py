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
* Compositor protection: the HIGH (DRM master) entity's fence deadlines reveal the
  vblank grid (period = smallest gap between deadlines), how long before vblank it
  submits, and its cost.  A job of a NORMAL/LOW hog (more than HOG_BURST of GPU time
  since it was last idle, BORE's burst time) whose estimated cost (EWMA of its entity's job times) would run past the compositor's
  latest start for the next vblank is held back if the compositor is due within
  HOLD_MAX; otherwise it runs.

Kernel mapping: vr/min_vr are drm_sched_entity / drm_sched_rq fields; idle detection
is the entity queue becoming empty with no job in flight; gpu_time comes from the
scheduled/finished fence timestamps; the vblank grid comes from the deadlines the
compositor's atomic commits set on its fences (dma_fence_set_deadline).
"""

from gpusim.api import PRIO_HIGH, PRIO_NORMAL, PolicyBase

SHIFT = (1, 2, 4, 7)
CREDIT = 16000        # µs of unused GPU time an idle entity may bank
HOLD_MAX = 2000       # longest the ring may idle waiting for the compositor (µs)
HOG_BURST = 4000      # GPU time since last idle beyond which an entity is a hog (µs)
MARGIN = 150          # doorbell + context switch + slack (µs)


def ewma(avg, x):
    return x if avg == 0 else avg + (x - avg) / 8.0


class Policy(PolicyBase):
    def __init__(self, sched):
        super().__init__(sched)
        self.min_vr = 0
        self.vblank = None      # last compositor deadline seen
        self.period = None
        self.comp_lead = 0.0    # compositor submits this long before its deadline
        self.comp_cost = 0.0

    def entity_init(self, e):
        e.ps.vr = self.min_vr
        e.ps.avg = 0.0
        e.ps.burst = 0

    def job_push(self, e, j):
        if e.idle_since_us is not None:
            e.ps.vr = max(e.ps.vr, self.min_vr - (CREDIT << SHIFT[e.priority]))

    def fence_deadline(self, e, j, t):
        if e.priority != PRIO_HIGH:
            return
        if self.vblank is not None and t > self.vblank:
            d = t - self.vblank
            self.period = d if self.period is None else min(self.period, d)
        if self.vblank is None or t > self.vblank:
            self.vblank = t
            self.comp_lead = ewma(self.comp_lead, t - self.sched.now)

    def job_done(self, e, j):
        e.ps.vr += j.gpu_us << SHIFT[e.priority]
        e.ps.avg = ewma(e.ps.avg, j.gpu_us)
        e.ps.burst += j.gpu_us
        if e.priority == PRIO_HIGH and j.deadline_us is not None:
            self.comp_cost = ewma(self.comp_cost, j.gpu_us)

    def entity_idle(self, e):
        e.ps.burst = 0

    def _comp_window(self, now):
        """(arrival, latest start) of the compositor's next frame, or None."""
        if self.period is None:
            return None
        v = self.vblank + self.period
        if v - self.comp_lead <= now:
            return None
        return v - self.comp_lead, v - self.comp_cost - MARGIN

    def select(self):
        s = self.sched
        now = s.now
        active = [e for e in s.entities if e.queue or e.inflight]
        if active:
            self.min_vr = max(self.min_vr, min(e.ps.vr for e in active))
        normal_busy = False
        for j in s.hw_jobs:
            o = s.lookup(j.ring, j.entity_id)
            if o is not None and o.priority >= PRIO_NORMAL:
                normal_busy = True
        win = self._comp_window(now) if s.name == "gfx" else None
        best = None
        best_key = None
        held = None
        for e in active:
            if not e.ready:
                continue
            if e.priority >= PRIO_NORMAL:
                if normal_busy:
                    continue
                if (win is not None and e.ps.burst > HOG_BURST and win[0] - now <= HOLD_MAX
                        and now + e.ps.avg > win[1]):
                    held = win[0]
                    continue
            key = (min(e.priority, PRIO_NORMAL), e.ps.vr, e.id)
            if best_key is None or key < best_key:
                best, best_key = e, key
        if best is None and held is not None:
            s.request_wakeup(held)
        return best
