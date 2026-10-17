"""CHEATING clairvoyant reference — calibration only, run with --trusted.

Uses hidden ground truth no kernel has: true job costs, which clients are interactive,
and the simulator's future event queue (upcoming frame submissions).  It estimates how
much headroom exists; it is NOT a legal policy and is not a strict lower bound either.

* urgent work (kernel moves, compositor, interactive clients): earliest ideal-finish first
* throughput work: least GPU time first (fair), but a hog job is only started if it
  finishes before the next predicted urgent arrival (minus a little slack)
"""

from gpusim.api import PolicyBase

URGENT = ("comp", "interactive", "kernel")
HORIZON_US = 60_000
SLACK_US = 800
IDLE_FRAC = 0.06      # may leave the ring idle (holding) at most this fraction of the time
IDLE_CAP_US = 20_000


class Policy(PolicyBase):
    def __init__(self, sched):
        super().__init__(sched)
        self.budget = 0.0
        self.last = 0
        self.hold_start = None

    def _refill(self, now):
        self.budget = min(IDLE_CAP_US, self.budget + (now - self.last) * IDLE_FRAC)
        self.last = now
        if self.hold_start is not None:
            self.budget -= now - self.hold_start
            self.hold_start = now if self.sched.credits_used == 0 else None

    def _next_urgent_arrival(self, now):
        sim = self.sched._sim
        best = None
        for t, _, fn, args in sim._heap:
            if t > now + HORIZON_US:
                continue
            obj = getattr(fn, "__self__", None)
            cls = getattr(obj, "cls", None)
            if cls not in ("comp", "interactive"):
                continue
            name = fn.__name__
            if name in ("_submit", "_first"):
                a = t
            elif name == "_wake":
                a = t + obj.cpu_us
            elif name == "_burst":
                a = t + obj.first_cpu_us
            elif name == "_tick":
                a = t + int(0.8 * obj.cpu_us) if hasattr(obj, "cpu_us") else t + 1000
            else:
                continue
            if best is None or a < best:
                best = a
        return best

    def select(self):
        s = self.sched
        now = s.now
        self._refill(now)
        ready = [e for e in s.entities if e.ready]
        if not ready:
            return None
        urg = [e for e in ready if e._client.cls in URGENT]
        if urg:
            return min(urg, key=lambda e: (e.head._ideal, e.id))
        e = min(ready, key=lambda e: (e._client.gpu_time, e.id))
        if s.name != "gfx":
            return e
        busy_until = now
        for j in s.hw_jobs:
            busy_until += j._cost if j._start is None else max(0, j._start + j._cost - busy_until)
        t_end = max(now, busy_until) + e.head._cost * (1 + 0.3 * e.head._gtt) + 60
        na = self._next_urgent_arrival(now)
        if na is not None and t_end > na + SLACK_US and (s.credits_used > 0 or self.budget > 0):
            if s.credits_used == 0 and self.hold_start is None:
                self.hold_start = now
            s.request_wakeup(max(na, now + 50) if s.credits_used else min(na, now + int(max(self.budget, 50))))
            return None
        self.hold_start = None
        return e
