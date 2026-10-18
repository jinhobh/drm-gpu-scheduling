"""Fair drm_sched policy with bounded sleeper credit (BORE/CFS-style interactivity).

* KERNEL and HIGH entities are strictly ahead; NORMAL/LOW share by virtual GPU time:
  vruntime += gpu_time << shift[prio]  (shifts KERNEL=1, HIGH=2, NORMAL=4, LOW=7).
* min_vr is a monotonic clock (cfs_rq->min_vruntime): the smallest vruntime among
  entities that have work queued or in flight.
* An entity waking from idle is placed at max(vr, min_vr - CREDIT << shift): it keeps
  up to CREDIT µs of GPU time it did not use while asleep (GENTLE_FAIR_SLEEPERS).
  Short-burst clients therefore jump ahead of hogs for a whole frame, while a hog
  that sleeps briefly between jobs only earns as much credit as it slept.
* HW queue depth: at most one NORMAL/LOW job on the ring at a time, and a hog only
  starts on an empty ring, so an urgent frame never waits behind two
  non-preemptible jobs one of which is a hog's.
* Compositor protection: the HIGH (DRM master) entity's fence deadlines reveal the
  vblank grid (period = smallest gap between deadlines), how long before vblank it
  submits, and its cost.  A job of a NORMAL/LOW hog (more than HOG_BURST of GPU time
  since it was last idle, or in its previous burst: BORE's burst time) whose estimated cost (EWMA of its entity's job times) would run past the compositor's
  latest start for the next vblank is held back if the compositor is due within
  HOLD_MAX; otherwise it runs.
* Interactive frames get the same protection: an entity that is not a hog starts a
  frame once per vblank during a burst, so its next frame is predicted one period
  after the current one started (EWMA frame cost), and must finish before the
  compositor latches it (vblank grid minus the compositor's lead).
* Starvation guard: a NORMAL/LOW head job ready for longer than AGE_US goes first
  among NORMAL/LOW and is never held.
* Holds draw on an idle budget that refills at IDLE_FRAC of wall time, so work
  conservation is bounded whatever the workload.

Kernel mapping: vr/min_vr are drm_sched_entity / drm_sched_rq fields; idle detection
is the entity queue becoming empty with no job in flight; gpu_time comes from the
scheduled/finished fence timestamps; the vblank grid comes from the deadlines the
compositor's atomic commits set on its fences (dma_fence_set_deadline).
"""

from gpusim.api import PRIO_HIGH, PRIO_NORMAL, PolicyBase

SHIFT = (1, 2, 4, 7)
CREDIT = 16000        # µs of unused GPU time an idle entity may bank
HOLD_MAX = 2000       # longest the ring may idle waiting for an urgent arrival (µs)
HOG_BURST = 4000      # GPU time since last idle beyond which an entity is a hog (µs)
AGE_US = (0, 0, 150000, 600000)   # a head job ready this long goes first (no starvation)
MARGIN = 150          # doorbell + context switch + slack (µs)
FRAME_GAP = 500       # pushes further apart than this start a new frame (µs)
JITTER = 500          # a predicted frame may arrive this late (µs)
IDLE_FRAC = 0.08      # holds may idle the ring at most this fraction of the time
IDLE_CAP = 20000      # ... banked up to this much (µs)


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
        self.budget = 0.0       # hold budget (µs of idle ring time)
        self.last_t = 0
        self.holding = False

    def entity_init(self, e):
        e.ps.vr = self.min_vr
        e.ps.avg = 0.0
        e.ps.burst = 0
        e.ps.last_burst = 0
        e.ps.last_push = None
        e.ps.frame_t = None     # when the entity's current frame started
        e.ps.fsum = 0
        e.ps.fcost = 0.0        # EWMA of GPU time per frame

    def job_push(self, e, j):
        now = self.sched.now
        if e.idle_since_us is not None:
            e.ps.vr = max(e.ps.vr, self.min_vr - (CREDIT << SHIFT[e.priority]))
        if e.ps.last_push is None or now - e.ps.last_push > FRAME_GAP:
            if e.ps.frame_t is not None:
                e.ps.fcost = ewma(e.ps.fcost, e.ps.fsum)
            e.ps.fsum = 0
            e.ps.frame_t = now
        e.ps.last_push = now

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
        e.ps.fsum += j.gpu_us
        if e.priority == PRIO_HIGH and j.deadline_us is not None:
            self.comp_cost = ewma(self.comp_cost, j.gpu_us)

    def entity_idle(self, e):
        e.ps.last_burst = e.ps.burst
        e.ps.burst = 0

    def _is_hog(self, e):
        return max(e.ps.burst, e.ps.last_burst) > HOG_BURST

    def _urgent(self, now):
        """(arrival, latest start) of frames predicted to arrive soon: the compositor's
        next frame, and the next frame of each interactive entity in a burst (one
        frame per vblank), which must finish before the compositor latches it."""
        out = []
        p = self.period
        if p is None:
            return out
        v = self.vblank + p
        if v - self.comp_lead > now:
            out.append((v - self.comp_lead, v - self.comp_cost - MARGIN))
        base = self.vblank - self.comp_lead - MARGIN
        for e in self.sched.entities:
            if e.priority < PRIO_NORMAL or e.queue or e.ps.frame_t is None or self._is_hog(e):
                continue
            a = e.ps.frame_t + p
            if a < now - JITTER:
                continue
            a = max(a, now)
            done = a + e.ps.fcost
            latch = base + -(-(done - base) // p) * p
            out.append((a, latch - e.ps.fcost))
        return out

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
        dt = now - self.last_t
        self.last_t = now
        self.budget = min(IDLE_CAP, self.budget + dt * IDLE_FRAC - (dt if self.holding else 0))
        urgent = self._urgent(now) if s.name == "gfx" and self.budget > 0 else ()
        best = None
        best_key = None
        held = None
        for e in active:
            if not e.ready:
                continue
            aged = now - e.head.ready_us > AGE_US[e.priority]
            if e.priority >= PRIO_NORMAL:
                if normal_busy:
                    continue
                if not aged and self._is_hog(e):
                    if s.credits_used:
                        continue
                    t = None
                    for a, ls in urgent:
                        if a - now <= HOLD_MAX and now + e.ps.avg > ls:
                            t = a if t is None else min(t, a)
                    if t is not None:
                        held = t if held is None else min(held, t)
                        continue
            key = (min(e.priority, PRIO_NORMAL), not aged, e.ps.vr, e.id)
            if best_key is None or key < best_key:
                best, best_key = e, key
        self.holding = best is None and held is not None
        if self.holding:
            s.request_wakeup(max(held, now + 100))
        return best
