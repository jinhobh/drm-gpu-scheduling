"""Discrete-event model of drm_sched + an amdgpu APU (gfx ring, SDMA ring, small VRAM).

FROZEN — the optimization agent must not edit this file.

Model summary
-------------
* Two drm_gpu_schedulers: ``gfx`` (all rendering) and ``sdma`` (TTM buffer moves at
  KERNEL priority, app texture uploads at NORMAL).  Each has ``credit_limit`` HW slots.
* drm_sched semantics: per-entity FIFO queues; only the head job of an entity can be
  picked and only once all its dependency fences signaled.  Dependencies on a job of
  the *same* scheduler only wait for that job's *scheduled* fence (ring is in-order).
* HW ring: in-order, non-preemptive.  Doorbell latency when an idle ring is fed,
  context-switch cost when consecutive jobs belong to different clients.
* gfx and sdma share DRAM bandwidth: when both are busy each runs slower.
* VRAM carveout with TTM-like LRU eviction.  Evictions are pipelined SDMA copies that
  must wait for every job (queued or running) still using the victim BO — so a deep
  queue of a hog delays the eviction a newcomer needs.  Optional moves back into VRAM
  are throttled like amdgpu_cs_get_threshold_for_moves (amdgpu.moverate, 8 MB/s by
  default).  Jobs whose BOs sit in GTT run slower.
"""

import heapq
import math
import random
from collections import deque

from .api import Namespace, PRIO_KERNEL


class PolicyError(Exception):
    pass


# --------------------------------------------------------------------------- fences
class Fence:
    __slots__ = ("signaled", "t", "_cbs", "_job")

    def __init__(self, job=None):
        self.signaled = False
        self.t = None
        self._cbs = []
        self._job = job


# --------------------------------------------------------------------------- jobs
class Job:
    __slots__ = (
        "id", "entity_id", "ring", "submit_us", "ready_us", "run_us", "done_us",
        "gpu_us", "deadline_us",
        "_ent", "_client", "_cost", "_deps", "_fin", "_sch", "_frame", "_ideal",
        "_start", "_gtt", "_waitf", "_tag",
    )

    def __init__(self, jid, ent, cost, now, tag):
        self.id = jid
        self.entity_id = ent.id
        self.ring = ent.ring
        self.submit_us = now
        self.ready_us = None
        self.run_us = None
        self.done_us = None
        self.gpu_us = None
        self.deadline_us = None
        self._ent = ent
        self._client = ent._client
        self._cost = cost
        self._deps = ()
        self._fin = Fence(self)
        self._sch = Fence(self)
        self._frame = None
        self._ideal = 0
        self._start = None
        self._gtt = 0.0
        self._waitf = None
        self._tag = tag

    @property
    def deps_pending(self):
        return sum(1 for f in self._deps if not f.signaled)

    @property
    def blocked_on(self):
        out = []
        for f in self._deps:
            if not f.signaled and f._job is not None:
                ref = (f._job.ring, f._job.entity_id)
                if ref not in out:
                    out.append(ref)
        return out

    def __hash__(self):
        return self.id

    def __repr__(self):
        return f"Job(id={self.id}, ent={self.entity_id}, ring={self.ring})"


# --------------------------------------------------------------------------- entities
class Entity:
    __slots__ = (
        "id", "ring", "client_id", "priority", "created_us", "ready", "inflight",
        "idle_since_us", "ps", "client_ps",
        "_q", "_sched", "_client", "_sim", "_alive", "_last_ideal", "_name",
    )

    def __init__(self, sim, sched, client, prio, name):
        self.id = sim._opaque_id()
        self.ring = sched.name
        self.client_id = client.id
        self.priority = prio
        self.created_us = sim.now
        self.ready = False
        self.inflight = 0
        self.idle_since_us = sim.now
        self.ps = Namespace()
        self.client_ps = client.ps
        self._q = deque()
        self._sched = sched
        self._client = client
        self._sim = sim
        self._alive = True
        self._last_ideal = 0
        self._name = name

    @property
    def head(self):
        return self._q[0] if self._q else None

    @property
    def queue(self):
        return tuple(self._q)

    @property
    def waiters(self):
        out = []
        for x in self._sim._all_ents:
            if not x._q or x.ready:
                continue
            h = x._q[0]
            for f in h._deps:
                if not f.signaled and f._job is not None and f._job._ent is self:
                    out.append(h)
                    break
        return out

    def _check_head(self):
        q = self._q
        if not q:
            self.ready = False
            return
        j = q[0]
        for f in j._deps:
            if not f.signaled:
                self.ready = False
                if j._waitf is not f:
                    j._waitf = f
                    f._cbs.append(lambda e=self, j=j: e._dep_cb(j))
                return
        j._waitf = None
        if not self.ready:
            self.ready = True
            j.ready_us = self._sim.now
            self._sched._policy.job_ready(self, j)
            self._sched._kick()

    def _dep_cb(self, j):
        if self._q and self._q[0] is j:
            self._check_head()

    def __hash__(self):
        return self.id

    def __repr__(self):
        return f"Entity(id={self.id}, ring={self.ring}, prio={self.priority})"


# --------------------------------------------------------------------------- schedulers
class Sched:
    """One drm_gpu_scheduler + its hardware ring."""

    def __init__(self, sim, name, credit_limit):
        self.name = name
        self.credit_limit = credit_limit
        self._sim = sim
        self._policy = None
        self._ents = []
        self._hw = deque()
        self._pass_pending = False
        self._begin_pending = False
        self._wakeups = {}
        self._cur = None
        self._rem = 0.0
        self._rate = 1.0
        self._last = 0
        self._ver = 0
        self._evt = False
        self._prev_done = 0
        self._last_client = None
        self._busy_us = 0

    # ---- policy-visible
    @property
    def now(self):
        return self._sim.now

    @property
    def credits_used(self):
        return len(self._hw)

    @property
    def hw_jobs(self):
        return tuple(self._hw)

    @property
    def entities(self):
        return tuple(self._ents)

    def request_wakeup(self, t_us):
        sim = self._sim
        t = int(t_us)
        if t <= sim.now:
            self._kick()
            return
        if t in self._wakeups:
            return
        self._wakeups[t] = True
        sim.at(t, self._wake, t)

    def lookup(self, ring, entity_id):
        s = self._sim.scheds.get(ring)
        if s is None:
            return None
        for e in s._ents:
            if e.id == entity_id:
                return e
        return None

    # ---- internals
    def _wake(self, t):
        self._wakeups.pop(t, None)
        self._kick()

    def _kick(self):
        if not self._pass_pending:
            self._pass_pending = True
            self._sim.after(self._sim.cfg["sched_wake_us"], self._pass)

    def _pass(self):
        sim = self._sim
        pol = self._policy
        hw = self._hw
        while len(hw) < self.credit_limit:
            e = pol.select()
            if e is None:
                break
            if e.__class__ is not Entity or e._sched is not self or not e._alive:
                raise PolicyError(f"{self.name}: select() returned {e!r}, not a live entity of this scheduler")
            if not e.ready:
                raise PolicyError(f"{self.name}: select() returned non-ready {e!r}")
            j = e._q.popleft()
            e.ready = False
            j.run_us = sim.now
            e.inflight += 1
            hw.append(j)
            sim._signal(j._sch)
            pol.job_run(e, j)
            e._check_head()
            if self._cur is None and not self._begin_pending:
                self._begin_pending = True
                sim.after(sim.cfg["submit_us"], self._begin)
        self._pass_pending = False

    def _begin(self):
        self._begin_pending = False
        if self._cur is not None or not self._hw:
            return
        sim = self._sim
        cfg = sim.cfg
        j = self._hw[0]
        work = j._cost * (1.0 + cfg["gtt_penalty"] * j._gtt)
        if j._client is not self._last_client:
            work += cfg["ctx_switch_us"]
        self._last_client = j._client
        j._start = sim.now
        self._cur = j
        self._rem = work
        self._last = sim.now
        self._rate = 1.0
        self._ver += 1
        self._evt = False
        sim._rebalance()

    def _complete(self, ver):
        if ver != self._ver or self._cur is None:
            return
        sim = self._sim
        now = sim.now
        j = self._cur
        self._cur = None
        self._busy_us += now - j._start
        popped = self._hw.popleft()
        assert popped is j
        j.done_us = now
        j.gpu_us = now - max(j.run_us, self._prev_done)
        self._prev_done = now
        e = j._ent
        e.inflight -= 1
        if sim._trace is not None:
            sim._trace.append(j)
        sim._account(j)
        sim._signal(j._fin)
        self._policy.job_done(e, j)
        if e._alive and not e._q and e.inflight == 0:
            e.idle_since_us = now
            self._policy.entity_idle(e)
        if self._hw:
            self._begin()
        else:
            sim._rebalance()
        self._kick()


# --------------------------------------------------------------------------- memory
class BO:
    __slots__ = ("id", "client", "size", "dom", "pinned", "lru", "use_f", "move_fs", "alive")

    def __init__(self, bid, client, size, pinned):
        self.id = bid
        self.client = client
        self.size = size
        self.dom = "G"
        self.pinned = pinned
        self.lru = 0
        self.use_f = None
        self.move_fs = []
        self.alive = True


class MemMgr:
    def __init__(self, sim):
        cfg = sim.cfg
        self.sim = sim
        self.cap = float(cfg["vram_mb"])
        self.used = 0.0
        self.bos = []
        self.bw = cfg["copy_mb_per_ms"] / 1000.0  # MB per µs
        self.rate = cfg["moverate_mb_s"] / 1e6  # MB per µs
        self.tokens = 0.0
        self.tok_cap = self.rate * 200000.0
        self.tok_last = 0
        self.evictions = 0
        self.evicted_mb = 0.0
        self.moved_in_mb = 0.0
        self.gtt_allocs_mb = 0.0

    @property
    def free(self):
        return self.cap - self.used

    def _copy(self, size, deps, tag):
        sim = self.sim
        live = [f for f in deps if f is not None and not f.signaled]
        j = sim.submit(sim.ttm_ent, size / self.bw, deps=live, tag=tag)
        return j._fin

    def _evict_for(self, need, exclude):
        if self.free >= need:
            return []
        cands = [b for b in self.bos if b.alive and b.dom == "V" and not b.pinned and b not in exclude]
        cands.sort(key=lambda b: (b.lru, b.id))
        picked = []
        freed = 0.0
        for b in cands:
            if self.free + freed >= need:
                break
            picked.append(b)
            freed += b.size
        if self.free + freed < need:
            return None
        fences = []
        for b in picked:
            deps = [b.use_f] + b.move_fs
            b.dom = "G"
            self.used -= b.size
            f = self._copy(b.size, deps, "evict")
            b.move_fs = [f]
            fences.append(f)
            self.evictions += 1
            self.evicted_mb += b.size
        return fences

    def alloc(self, client, size, pinned=False, prefer_vram=True):
        sim = self.sim
        bo = BO(sim._next_id(), client, float(size), pinned)
        bo.lru = sim.now
        self.bos.append(bo)
        if prefer_vram:
            fences = self._evict_for(bo.size, ())
            if fences is not None:
                bo.dom = "V"
                self.used += bo.size
                bo.move_fs = fences
                return bo
        self.gtt_allocs_mb += bo.size
        return bo

    def free_bo(self, bo):
        if not bo.alive:
            return
        bo.alive = False
        if bo.dom == "V":
            self.used -= bo.size
        self.bos = [b for b in self.bos if b.alive]

    def validate(self, bos, job):
        """amdgpu CS BO validation: returns the move fences the job must wait for."""
        now = self.sim.now
        self.tokens = min(self.tok_cap, self.tokens + (now - self.tok_last) * self.rate)
        self.tok_last = now
        deps = []
        tot = 0.0
        gtt = 0.0
        for b in bos:
            b.lru = now
            if b.dom == "G" and not b.pinned and self.tokens > 0.0:
                ev0 = self.evicted_mb
                fences = self._evict_for(b.size, bos)
                if fences is not None:
                    f = self._copy(b.size, fences + [b.use_f] + b.move_fs, "move_in")
                    b.dom = "V"
                    self.used += b.size
                    b.move_fs = [f]
                    # amdgpu charges evictions and the move itself against the budget
                    self.tokens -= b.size + (self.evicted_mb - ev0)
                    self.moved_in_mb += b.size
            for f in b.move_fs:
                if not f.signaled:
                    deps.append(f)
            tot += b.size
            if b.dom == "G":
                gtt += b.size
            b.use_f = job._fin
        job._gtt = gtt / tot if tot > 0 else 0.0
        return deps


# --------------------------------------------------------------------------- display
class Display:
    def __init__(self, cfg):
        self.period = int(round(1e6 / cfg["refresh_hz"]))
        self.lead = int(self.period * cfg["lead_frac"])
        self.windows = []  # (start, end, surfaces, effect_us)
        self._sorted = True

    def add_window(self, start, end, surfaces=1, effect_us=0.0):
        self.windows.append((int(start), int(end), surfaces, effect_us))
        self._sorted = False

    def active(self, t):
        if not self._sorted:
            self.windows.sort()
            self._sorted = True
        surf = 0
        eff = 0.0
        anyw = False
        for s, e, n, x in self.windows:
            if s > t:
                break
            if t < e:
                anyw = True
                surf += n
                eff += x
        return anyw, surf, eff

    def next_vblank(self, t):
        p = self.period
        return ((int(t) + p - 1) // p) * p

    def vblank_idx(self, t):
        p = self.period
        return -(-int(t) // p)

    def latch_idx(self, t):
        p = self.period
        return -(-(int(t) + self.lead) // p)


# --------------------------------------------------------------------------- frames
class FrameRec:
    """Hidden ground-truth record of one interactive frame opportunity."""

    __slots__ = ("client", "kind", "w", "grid", "opp", "job", "skipped", "cls")

    def __init__(self, client, kind, w, grid, opp, cls):
        self.client = client
        self.kind = kind  # 'comp' | 'frame' | 'first'
        self.w = w
        self.grid = grid  # 'vblank' | 'latch'
        self.opp = opp
        self.job = None
        self.skipped = False
        self.cls = cls  # 'comp' | 'burst' | 'steady' | 'resp'


# --------------------------------------------------------------------------- clients
class Client:
    def __init__(self, sim, name, cls):
        self.sim = sim
        self.id = sim._opaque_id()
        self.name = name
        self.cls = cls  # 'comp' | 'interactive' | 'throughput' | 'kernel'
        self.ps = Namespace()
        self.work_done = 0.0
        self.gpu_time = 0.0
        self.jobs_done = 0
        self.max_wait = 0
        self.mimic = False
        self.ents = []
        sim.clients.append(self)

    def new_entity(self, ring, prio):
        return self.sim.new_entity(self, ring, prio)


# --------------------------------------------------------------------------- simulator
DEFAULT_CFG = dict(
    duration_s=20.0,
    warmup_s=1.0,
    refresh_hz=120,
    lead_frac=0.4,          # compositor latches clients this far (fraction of a frame) before vblank
    vram_mb=512,            # APU carveout (this machine: 512 MiB)
    credits=2,              # amdgpu sched_hw_submission
    submit_us=25,           # doorbell + CP fetch when feeding an idle ring
    sched_wake_us=15,       # drm_sched work item latency
    ctx_switch_us=40,       # switching client context on the ring
    copy_mb_per_ms=12.0,    # SDMA copy bandwidth
    gtt_penalty=0.30,       # slowdown for a job whose whole working set is in GTT
    gfx_slow=0.15,          # gfx slowdown while SDMA copies run (shared DRAM bw)
    sdma_slow=0.35,         # SDMA slowdown while gfx runs
    moverate_mb_s=8.0,      # amdgpu.moverate default (optional GTT->VRAM moves)
    client_deadline_hints=False,
    id_seed="ids",          # entity/client ids are opaque random ints, different every run
)


class Sim:
    def __init__(self, cfg, policy_factory):
        self.cfg = dict(DEFAULT_CFG)
        self.cfg.update(cfg)
        self.now = 0
        self.end = int(self.cfg["duration_s"] * 1e6)
        self.warmup = int(self.cfg["warmup_s"] * 1e6)
        self._heap = []
        self._seq = 0
        self._ids = 0
        self._id_rng = random.Random(self.cfg["id_seed"])
        self._trace = [] if self.cfg.get("trace") else None
        self._used_ids = {}
        self.clients = []
        self._all_ents = []
        self.frames = []
        self.display = Display(self.cfg)
        self.scheds = {
            "gfx": Sched(self, "gfx", self.cfg["credits"]),
            "sdma": Sched(self, "sdma", self.cfg["credits"]),
        }
        for s in self.scheds.values():
            s._policy = policy_factory(s)
        self.mem = MemMgr(self)
        self.ttm_client = Client(self, "ttm", "kernel")
        self.ttm_ent = self.new_entity(self.ttm_client, "sdma", PRIO_KERNEL)

    # ---- event loop
    def _next_id(self):
        self._ids += 1
        return self._ids

    def _opaque_id(self):
        while True:
            x = self._id_rng.randrange(1, 1 << 31)
            if x not in self._used_ids:
                self._used_ids[x] = True
                return x

    def at(self, t, fn, *args):
        self._seq += 1
        heapq.heappush(self._heap, (int(t), self._seq, fn, args))

    def after(self, dt, fn, *args):
        self.at(self.now + dt, fn, *args)

    def run(self):
        heap = self._heap
        end = self.end
        pop = heapq.heappop
        while heap and heap[0][0] <= end:
            t, _, fn, args = pop(heap)
            self.now = t
            fn(*args)
        self.now = end

    def _signal(self, f):
        f.signaled = True
        f.t = self.now
        cbs = f._cbs
        f._cbs = None
        for cb in cbs:
            cb()

    def on_done(self, job, cb):
        """Client-side wait on a job's finished fence."""
        f = job._fin
        if f.signaled:
            cb()
        else:
            f._cbs.append(cb)

    # ---- entities
    def new_entity(self, client, ring, prio):
        s = self.scheds[ring]
        e = Entity(self, s, client, prio, f"{client.name}:{ring}")
        # kernel gives no ordering guarantee among entities; don't let list position leak identity
        s._ents.insert(self._id_rng.randrange(len(s._ents) + 1), e)
        self._all_ents.append(e)
        client.ents.append(e)
        s._policy.entity_init(e)
        return e

    def fini_entity(self, e):
        if not e._alive:
            return
        if e._q or e.inflight:
            self.after(20000, self.fini_entity, e)
            return
        e._alive = False
        s = e._sched
        s._ents.remove(e)
        self._all_ents.remove(e)
        s._policy.entity_fini(e)

    # ---- CS ioctl
    def submit(self, ent, cost_us, bos=(), deps=(), frame=None, deadline_us=None, tag=""):
        if not ent._alive:
            raise RuntimeError("submit to dead entity")
        now = self.now
        sched = ent._sched
        j = Job(self._next_id(), ent, float(cost_us), now, tag)
        mdeps = self.mem.validate(bos, j) if bos else []
        ideal = max(now, ent._last_ideal)
        out = []
        for f in deps:
            dj = f._job
            if dj is not None and dj._client is ent._client and dj._ideal > ideal:
                ideal = dj._ideal
            if f.signaled:
                continue
            if dj is not None and dj._ent._sched is sched and f is dj._fin:
                f = dj._sch
                if f.signaled:
                    continue
            out.append(f)
        for f in mdeps:
            if not f.signaled:
                dj = f._job
                if dj is not None and dj._ent._sched is sched:
                    f = dj._sch
                    if f.signaled:
                        continue
                out.append(f)
        j._deps = out
        j._ideal = ideal + cost_us
        ent._last_ideal = j._ideal
        if frame is not None:
            j._frame = frame
            frame.job = j
        was_empty = not ent._q
        ent._q.append(j)
        pol = sched._policy
        pol.job_push(ent, j)
        ent.idle_since_us = None
        if deadline_us is not None:
            j.deadline_us = int(deadline_us)
            pol.fence_deadline(ent, j, j.deadline_us)
        if was_empty:
            ent._check_head()
        return j

    def set_deadline(self, job, t):
        if job.done_us is not None:
            return
        if job.deadline_us is None or t < job.deadline_us:
            job.deadline_us = int(t)
            job._ent._sched._policy.fence_deadline(job._ent, job, job.deadline_us)

    # ---- rings
    def _rebalance(self):
        now = self.now
        g = self.scheds["gfx"]
        s = self.scheds["sdma"]
        both = g._cur is not None and s._cur is not None
        cfg = self.cfg
        for r, slow in ((g, cfg["gfx_slow"]), (s, cfg["sdma_slow"])):
            if r._cur is None:
                continue
            r._rem -= (now - r._last) * r._rate
            r._last = now
            rate = 1.0 / (1.0 + slow) if both else 1.0
            if rate != r._rate or not r._evt:
                r._rate = rate
                r._ver += 1
                r._evt = True
                rem = r._rem if r._rem > 0 else 0.0
                self.at(now + int(math.ceil(rem / rate)), r._complete, r._ver)

    def _account(self, j):
        c = j._client
        wait = j.run_us - (j.ready_us if j.ready_us is not None else j.run_us)
        if j.done_us >= self.warmup:
            c.work_done += j._cost
            c.gpu_time += j.gpu_us
            c.jobs_done += 1
            if wait > c.max_wait:
                c.max_wait = wait

    # ---- frame bookkeeping (hidden from policies)
    def frame(self, client, kind, w, grid, opp, cls):
        rec = FrameRec(client, kind, w, grid, opp, cls)
        self.frames.append(rec)
        return rec
