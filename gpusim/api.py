"""Policy-facing API of the drm_sched simulator.  FROZEN — do not edit.

A policy is one Python file defining ``class Policy(PolicyBase)``.  One
instance is created per ``drm_gpu_scheduler`` (per ring: ``"gfx"`` and
``"sdma"``), exactly like a kernel policy would be per-scheduler.

Everything a policy can see is something the kernel either already has in
``drm_sched`` / ``dma_fence`` or could plausibly plumb in cheaply.  Ground-truth
information (true job cost, which client is "interactive", future arrivals,
scenario identity) is deliberately NOT exposed.  Attributes whose names start
with ``_`` are simulator internals; the loader rejects policies that touch them.

All times are integer microseconds (µs) of simulated time.

Priorities mirror ``enum drm_sched_priority`` (lower number = more important)::

    PRIO_KERNEL = 0   # TTM buffer moves, page-table updates
    PRIO_HIGH   = 1   # the compositor (DRM master) gets this
    PRIO_NORMAL = 2   # ordinary clients
    PRIO_LOW    = 3   # clients that asked to be background

-------------------------------------------------------------------------------
Sched  (``self.sched``; one per ring — kernel: struct drm_gpu_scheduler)
-------------------------------------------------------------------------------
    sched.name            "gfx" or "sdma"
    sched.now             current time (µs)
    sched.credit_limit    max jobs on the HW ring at once (amdgpu sched_hw_submission = 2)
    sched.credits_used    jobs currently on the HW ring (pushed by run_job, not finished)
    sched.hw_jobs         tuple of Jobs on the HW ring in execution order ([0] is executing
                          or about to)
    sched.entities        tuple of live Entities attached to this scheduler
    sched.request_wakeup(t_us)
                          ask for another select() pass at time t_us.  Use this if you
                          deliberately return None while work is ready (holding the ring).
    sched.lookup(ring, entity_id) -> Entity | None
                          look up an entity on any scheduler (for dependency-chain walking)

-------------------------------------------------------------------------------
Entity  (kernel: struct drm_sched_entity — one per (client context, ring))
-------------------------------------------------------------------------------
    e.id                  unique int
    e.ring                "gfx" / "sdma"
    e.client_id           int identifying the owning client (drm_file / process)
    e.priority            0..3 (see above)
    e.created_us          creation time
    e.ready               True iff the head job exists and all its dependencies are signaled
                          (only ready entities may be returned from select())
    e.head                head Job of the entity queue, or None
    e.queue               tuple of queued Jobs (head first), not yet run
    e.inflight            number of this entity's jobs on the HW ring
    e.idle_since_us       time the entity went idle (empty queue AND nothing in flight),
                          or None while busy.  Inside job_push() it still holds the value
                          from before the push, so you can compute how long it slept.
    e.waiters             list of Jobs (any ring) whose head-of-queue dependency is an
                          unsignaled fence of one of this entity's jobs (priority-inversion
                          detection, e.g. a TTM eviction copy waiting on this entity)
    e.ps                  per-entity scratch namespace for the policy (writable)
    e.client_ps           per-client scratch namespace shared by all of that client's
                          entities on all rings (writable; prefix your keys per ring)

-------------------------------------------------------------------------------
Job  (kernel: struct drm_sched_job + its scheduler fences)
-------------------------------------------------------------------------------
    j.id, j.entity_id, j.ring
    j.submit_us           time it was pushed to the entity (drm_sched_entity_push_job)
    j.ready_us            time it became head-of-queue with all deps signaled (or None)
    j.run_us              time run_job() pushed it to the HW ring (or None)
    j.done_us             time its finished fence signaled (or None)
    j.gpu_us              measured execution time, valid once done:
                          done_us - max(run_us, previous job's done_us on this ring)
    j.deadline_us         dma_fence_set_deadline() hint on its finished fence, or None.
                          The compositor sets this to the target vblank on its own jobs;
                          in some scenarios it also sets it on client frame fences.
    j.deps_pending        number of unsignaled dependencies
    j.blocked_on          list of (ring, entity_id) owning the unsignaled scheduler-fence deps

-------------------------------------------------------------------------------
Hooks  (all optional except select)
-------------------------------------------------------------------------------
    entity_init(e)            drm_sched_entity_init
    entity_fini(e)            entity destroyed (client exited)
    job_push(e, j)            drm_sched_entity_push_job (job may still have pending deps)
    job_ready(e, j)           j became head-of-queue with all deps signaled
    job_run(e, j)             run_job: j popped from e and pushed to the HW ring
    job_done(e, j)            j's finished fence signaled; j.gpu_us now valid
    entity_idle(e)            e has no queued jobs and nothing in flight
    fence_deadline(e, j, t)   a deadline hint t was set on j's finished fence
    select() -> Entity|None   drm_sched_select_entity: called while credits are free.
                              Return a READY entity of this scheduler, or None to leave the
                              ring as-is (call sched.request_wakeup() if you want a retry
                              before the next natural event).

HW model notes: the ring executes jobs strictly in run order and cannot preempt a job
once it has been pushed (no MCBP on the kernel-queue path).  Switching between clients
on the ring costs a context switch.  A ring that went idle pays a doorbell/fetch latency
when a new job is pushed, so keeping only one job in flight can leave small bubbles.
"""

PRIO_KERNEL = 0
PRIO_HIGH = 1
PRIO_NORMAL = 2
PRIO_LOW = 3
PRIO_NAMES = ("KERNEL", "HIGH", "NORMAL", "LOW")
NUM_PRIOS = 4


class Namespace:
    """Free-form attribute bag (like types.SimpleNamespace)."""

    def __repr__(self):
        items = ", ".join(f"{k}={v!r}" for k, v in sorted(self.__dict__.items()))
        return f"Namespace({items})"


class PolicyBase:
    """Base class for scheduling policies.  Subclass as ``class Policy(PolicyBase)``."""

    def __init__(self, sched):
        self.sched = sched

    def entity_init(self, e):
        pass

    def entity_fini(self, e):
        pass

    def job_push(self, e, j):
        pass

    def job_ready(self, e, j):
        pass

    def job_run(self, e, j):
        pass

    def job_done(self, e, j):
        pass

    def entity_idle(self, e):
        pass

    def fence_deadline(self, e, j, t):
        pass

    def select(self):
        raise NotImplementedError
