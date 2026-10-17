"""Legacy drm_sched round-robin policy (DRM_SCHED_POLICY_RR).

Strict priority between run queues; within a run queue, rotate through entities
starting after the one picked last, taking the first ready one.
"""

from gpusim.api import NUM_PRIOS, PolicyBase


class Policy(PolicyBase):
    def __init__(self, sched):
        super().__init__(sched)
        self.last = [None] * NUM_PRIOS  # entity id picked last per run queue

    def select(self):
        ents = self.sched.entities
        for prio in range(NUM_PRIOS):
            rq = [e for e in ents if e.priority == prio]
            if not rq:
                continue
            start = 0
            for i, e in enumerate(rq):
                if e.id == self.last[prio]:
                    start = i + 1
                    break
            n = len(rq)
            for k in range(n):
                e = rq[(start + k) % n]
                if e.ready:
                    self.last[prio] = e.id
                    return e
        return None
