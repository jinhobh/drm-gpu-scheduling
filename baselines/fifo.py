"""Stock drm_sched FIFO policy (DRM_SCHED_POLICY_FIFO, the default on this 7.0 kernel).

Strict priority between run queues (KERNEL > HIGH > NORMAL > LOW); within a run queue
pick the ready entity whose head job was submitted earliest.  This is the reference
policy for the throughput constraint.
"""

from gpusim.api import PolicyBase


class Policy(PolicyBase):
    def select(self):
        best = None
        best_key = None
        for e in self.sched.entities:
            if not e.ready:
                continue
            key = (e.priority, e.head.submit_us, e.id)
            if best_key is None or key < best_key:
                best, best_key = e, key
        return best
