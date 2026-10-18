"""Fair drm_sched policy with bounded sleeper credit (BORE/CFS-style interactivity).

* KERNEL and HIGH entities are strictly ahead; NORMAL/LOW share by virtual GPU time:
  vruntime += cost << shift[prio]  (shifts KERNEL=1, HIGH=2, NORMAL=4, LOW=7), where
  cost = gpu_time * (1 + gpu_time / LONG): a long non-preemptible job also costs
  everyone else head-of-line latency, so it is charged a little extra.
* min_vr is a monotonic clock (cfs_rq->min_vruntime): the smallest vruntime among
  entities that have work queued or in flight.
* An entity waking from idle is placed at max(vr, min_vr - CREDIT << shift): it keeps
  up to CREDIT µs of GPU time it did not use while asleep (GENTLE_FAIR_SLEEPERS).
  Short-burst clients therefore jump ahead of hogs for a whole frame, while a hog
  that sleeps briefly between jobs only earns as much credit as it slept.
* Within NORMAL/LOW, entities that are not hogs go before hogs (vruntime orders each
  group); hogs cannot starve thanks to the starvation guard below.
* HW queue depth: at most one NORMAL/LOW entity on the ring at a time (a non-hog may
  queue its own next job behind its running one), and a hog only starts on an empty
  ring, so an urgent frame never waits behind two non-preemptible jobs.
* Hogs: BORE's burst time is CPU time between sleeps; the GPU analogue used here is
  the GPU time an entity received between its own frame submissions (pushes more
  than FRAME_GAP apart), i.e. while its client was not back on the CPU producing the
  next frame.  It does not grow when the scheduler starves a client, unlike time
  since idle.  An entity is a hog if its current or previous frame exceeded
  HOG_BURST, or it keeps more than HOG_QUEUE jobs queued.
* Compositor protection: the HIGH (DRM master) entity's fence deadlines reveal the
  vblank grid (period = smallest gap between deadlines), how long before vblank it
  submits, and its cost; its next frame is due by the next vblank, or before its
  next wake-up if it is too heavy to make the vblank.
* Interactive frames: an entity that is not a hog starts a frame every k vblanks
  during a burst (k from its last frame interval), so its next frame is predicted k
  periods after the current one started (EWMA frame cost), and must finish before the compositor latches it
  (vblank grid minus the compositor's lead).
* Holds: a hog's job (cost = EWMA of its job times) is held back if running it first
  would make the predicted urgent frames infeasible (EDF walk in arrival order) while
  they are feasible without it, and the first of them is due within HOLD_MAX.  If the
  arrival is further out, a conflicting hog job shorter than a vblank period is only
  deferred: any other ready job goes first (no idling).  Longer jobs can never fit
  between compositor frames and keep their fair turn, unless a client's animation
  is running: then they are deferred too and catch up (lowest vruntime) after it.
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
CREDIT = 100000       # µs of unused GPU time an idle entity may bank
LONG = 50000          # a job of length L is charged L * (1 + L / LONG) of vruntime
HOLD_MAX = 2000       # longest the ring may idle waiting for an urgent arrival (µs)
HOG_BURST = 3000      # GPU time per frame beyond which an entity is a hog (µs)
HOG_QUEUE = 4         # ... or more jobs than this queued
AGE_US = (0, 0, 150000, 600000)   # a head job ready this long goes first (no starvation)
MARGIN = 300          # latch pad + doorbell + context switch (µs)
FRAME_GAP = 500       # pushes further apart than this start a new frame (µs)
JITTER = 500          # a predicted frame may arrive this late (µs)
IDLE_FRAC = 0.10      # holds may idle the ring at most this fraction of the time
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
        e.ps.last_push = None
        e.ps.frame_t = None     # when the entity's current frame started
        e.ps.fsum = 0           # GPU time received since the current frame started
        e.ps.last_f = 0         # ... during the previous frame
        e.ps.fcost = 0.0        # EWMA of GPU time per frame
        e.ps.fk = 1             # frame interval in vblank periods

    def job_push(self, e, j):
        now = self.sched.now
        if e.idle_since_us is not None:
            e.ps.vr = max(e.ps.vr, self.min_vr - (CREDIT << SHIFT[e.priority]))
        if e.ps.last_push is None or now - e.ps.last_push > FRAME_GAP:
            if e.ps.frame_t is not None:
                e.ps.fcost = ewma(e.ps.fcost, e.ps.fsum)
                if self.period:
                    k = round((now - e.ps.frame_t) / self.period)
                    if k <= 4:
                        e.ps.fk = max(1, k)
            e.ps.last_f = e.ps.fsum
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
        e.ps.vr += (j.gpu_us + j.gpu_us * j.gpu_us // LONG) << SHIFT[e.priority]
        e.ps.avg = ewma(e.ps.avg, j.gpu_us)
        e.ps.fsum += j.gpu_us
        if e.priority == PRIO_HIGH and j.deadline_us is not None:
            self.comp_cost = ewma(self.comp_cost, j.gpu_us)

    def _is_hog(self, e):
        return max(e.ps.fsum, e.ps.last_f) > HOG_BURST or len(e.queue) > HOG_QUEUE

    def _urgent(self, now):
        """(arrival, cost, deadline) of frames predicted to arrive soon, by arrival: the
        compositor's next frame, and the next frame of each interactive entity in a
        burst (one frame per vblank), which must finish before the compositor latches it."""
        out = []
        p = self.period
        if p is None:
            return out
        v = self.vblank + p
        a = v - self.comp_lead
        c = self.comp_cost + MARGIN
        if a > now:
            if a + c > v:
                v += p - self.comp_lead     # can't make this vblank: due before next wake
            out.append((a, c, v, False))
        base = self.vblank - self.comp_lead - MARGIN
        for e in self.sched.entities:
            if e.priority < PRIO_NORMAL or e.queue or e.ps.frame_t is None or self._is_hog(e):
                continue
            a = e.ps.frame_t + e.ps.fk * p
            if a < now - JITTER:
                continue
            a = max(a, now)
            done = a + e.ps.fcost
            out.append((a, e.ps.fcost + MARGIN, base + -(-(done - base) // p) * p, True))
        out.sort()
        return out

    @staticmethod
    def _feasible(t, urgent):
        """Do the urgent frames all meet their deadlines if the ring is free from t?"""
        for a, c, d, _ in urgent:
            t = max(t, a) + c
            if t > d:
                return False
        return True

    def select(self):
        s = self.sched
        now = s.now
        active = [e for e in s.entities if e.queue or e.inflight]
        if active:
            self.min_vr = max(self.min_vr, min(e.ps.vr for e in active))
        normal_busy = None      # id of the NORMAL/LOW entity with a job on the ring
        for j in s.hw_jobs:
            o = s.lookup(j.ring, j.entity_id)
            if o is not None and o.priority >= PRIO_NORMAL:
                normal_busy = o.id
        dt = now - self.last_t
        self.last_t = now
        self.budget = min(IDLE_CAP, self.budget + dt * IDLE_FRAC - (dt if self.holding else 0))
        urgent = self._urgent(now) if s.name == "gfx" else ()
        best = None
        best_key = None
        held = None
        for e in active:
            if not e.ready:
                continue
            aged = now - e.head.ready_us > AGE_US[e.priority]
            defer = hog = False
            if e.priority >= PRIO_NORMAL:
                hog = self._is_hog(e)
                if normal_busy is not None and (hog or normal_busy != e.id):
                    continue
                if not aged and hog:
                    if s.credits_used:
                        continue
                    if (urgent and not self._feasible(now + e.ps.avg, urgent)
                            and self._feasible(now, urgent)):
                        if self.budget > 0 and urgent[0][0] - now <= HOLD_MAX:
                            held = urgent[0][0]
                            continue
                        defer = e.ps.avg < self.period or any(u[3] for u in urgent)
            key = (min(e.priority, PRIO_NORMAL), not aged, hog, defer, e.ps.vr, e.id)
            if best_key is None or key < best_key:
                best, best_key = e, key
        if best is not None and best_key[3] and held is not None:
            best = None
        self.holding = best is None and held is not None
        if self.holding:
            s.request_wakeup(max(held, now + 100))
        return best
