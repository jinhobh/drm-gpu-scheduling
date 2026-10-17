"""Metrics and the training objective.  FROZEN — the optimization agent must not edit this file.

OBJECTIVE (lower is better)
===========================
Per run:

    jank = 100 * sum_c W[c] * missrate[c] / sum_c W[c]        (over classes present)

      burst  (W=0.35)  frames of spontaneous animations (widgets, scrolling, glass,
                       app intro animations) that missed the vblank they would have hit
                       on an otherwise idle GPU, plus frames dropped because 2 were
                       already in flight.
      resp   (W=0.30)  responsiveness: first frame after input / app launch, graded by
                       how many vblanks late it was vs. the unloaded ideal: min(late,4)/4.
      comp   (W=0.20)  compositor frames that missed their vblank (whole-screen stutter).
      steady (W=0.15)  steady interactive streams (capped game, video) that missed.

    penalty = tput + fair + mimic + starve        (each is 0 when the constraint holds)

      tput    200 * max(0, 0.90 - T)    work conservation: T = total useful GPU work done
                                        (all clients, nominal cost of completed jobs) / the
                                        same for stock FIFO on the same run.  Only checked
                                        when a throughput client exists.  Shifting GPU time
                                        from a hog to frames FIFO would have dropped is free;
                                        leaving the GPU idle (holding) or thrashing is not.
      fair    200 * max(0, 0.95 - J)    J = Jain index of GPU time among backlogged
                                        throughput clients     [fair_hogs only]
      mimic   200 * max(0, S - 0.55)    S = disguised hog's share of contended GPU time
                                        [mimic_attack only]
      starve  up to 50 each:   worst ready->run wait of any KERNEL/HIGH/NORMAL job beyond
                               250 ms (full at 1 s); of any LOW job beyond 1 s (full at 3 s)

    run_score = jank + penalty
    scenario_score = mean over seeds;  jank_score = mean over scenarios.

"Missed" is always measured against the vblank the frame would hit with the GPU to
itself, so a frame that is inherently too heavy is not charged — only contention is.
"""

CLASS_W = {"burst": 0.35, "resp": 0.30, "comp": 0.20, "steady": 0.15}
TPUT_FLOOR = 0.90
FAIR_FLOOR = 0.95
MIMIC_CEIL = 0.55
STARVE = {"normal": (250_000, 1_000_000), "low": (1_000_000, 3_000_000)}


def collect(sim):
    """Extract raw metrics from a finished simulation (uses hidden ground truth)."""
    d = sim.display
    end = sim.end
    warm = sim.warmup
    acc = {c: [0.0, 0.0] for c in CLASS_W}
    per_client = {}
    first_late_ms = []
    app_ttff_ms = []
    for rec in sim.frames:
        if rec.opp < warm:
            continue
        j = rec.job
        if rec.skipped:
            m = 1.0
        elif j is None or j.done_us is None:
            ideal = j._ideal if j is not None else rec.opp
            if ideal >= end - 100_000:
                continue
            m = 1.0
            if rec.kind == "first":
                first_late_ms.append((end - ideal) / 1000.0)
        else:
            idx = d.vblank_idx if rec.grid == "vblank" else d.latch_idx
            late = idx(j.done_us) - idx(j._ideal)
            if rec.kind == "first":
                m = min(max(late, 0), 4) / 4.0
                first_late_ms.append(max(0, j.done_us - j._ideal) / 1000.0)
                if rec.client.name.startswith("app"):
                    app_ttff_ms.append((j.done_us - rec.opp) / 1000.0)
            else:
                m = 1.0 if late > 0 else 0.0
        a = acc[rec.cls]
        a[0] += rec.w * m
        a[1] += rec.w
        pc = per_client.setdefault(rec.client.name, [0.0, 0])
        pc[0] += m
        pc[1] += 1

    miss = {c: (a[0] / a[1] if a[1] > 0 else None) for c, a in acc.items()}

    tput_clients = [c for c in sim.clients if c.cls == "throughput"]
    gpu_time = {c.name: c.gpu_time for c in tput_clients}
    work = {c.name: c.work_done for c in tput_clients}
    useful = sum(c.work_done for c in sim.clients if c.cls != "kernel")

    wait_normal = 0
    wait_low = 0
    for c in sim.clients:
        if not c.ents:
            continue
        prio = min(e.priority for e in c.ents)
        if prio >= 3:
            wait_low = max(wait_low, c.max_wait)
        else:
            wait_normal = max(wait_normal, c.max_wait)
    # jobs still stuck in queues at the end count as waiting until the end
    for s in sim.scheds.values():
        for e in s._ents:
            if e.ready and e._q and e._q[0].ready_us is not None:
                w = end - e._q[0].ready_us
                if e.priority >= 3:
                    wait_low = max(wait_low, w)
                else:
                    wait_normal = max(wait_normal, w)

    mimic_share = None
    mim = [c for c in tput_clients if c.mimic]
    if mim:
        tot = sum(c.gpu_time for c in tput_clients)
        mimic_share = sum(c.gpu_time for c in mim) / tot if tot > 0 else 0.0

    xs = [c.gpu_time for c in tput_clients]
    jain = (sum(xs) ** 2 / (len(xs) * sum(x * x for x in xs))) if xs and sum(xs) > 0 else None

    span = max(1, end - warm)
    return dict(
        miss=miss,
        per_client={k: (v[0] / v[1] if v[1] else 0.0, v[1]) for k, v in per_client.items()},
        tput_work=sum(work.values()),
        useful_work=useful if tput_clients else 0.0,
        work=work,
        gpu_time=gpu_time,
        jain=jain,
        mimic_share=mimic_share,
        wait_normal_ms=wait_normal / 1000.0,
        wait_low_ms=wait_low / 1000.0,
        first_late_ms=first_late_ms,
        app_ttff_ms=app_ttff_ms,
        gfx_util=sim.scheds["gfx"]._busy_us / end,
        sdma_util=sim.scheds["sdma"]._busy_us / end,
        evictions=sim.mem.evictions,
        evicted_mb=sim.mem.evicted_mb,
        moved_in_mb=sim.mem.moved_in_mb,
        span_us=span,
    )


def _ramp(x, lo, hi):
    if x <= lo:
        return 0.0
    return min(1.0, (x - lo) / (hi - lo))


def score_run(m, ref_work, checks):
    """Combine raw metrics of one run into (score, breakdown)."""
    num = 0.0
    den = 0.0
    for c, w in CLASS_W.items():
        r = m["miss"][c]
        if r is not None:
            num += w * r
            den += w
    jank = 100.0 * num / den if den > 0 else 0.0

    pen = {}
    T = None
    if ref_work and ref_work > 0:
        T = m["useful_work"] / ref_work
        pen["tput"] = 200.0 * max(0.0, TPUT_FLOOR - T)
    if "fair" in checks and m["jain"] is not None:
        pen["fair"] = 200.0 * max(0.0, FAIR_FLOOR - m["jain"])
    if "mimic" in checks and m["mimic_share"] is not None:
        pen["mimic"] = 200.0 * max(0.0, m["mimic_share"] - MIMIC_CEIL)
    lo, hi = STARVE["normal"]
    s = 50.0 * _ramp(m["wait_normal_ms"] * 1000, lo, hi)
    lo, hi = STARVE["low"]
    s += 50.0 * _ramp(m["wait_low_ms"] * 1000, lo, hi)
    pen["starve"] = s
    penalty = sum(pen.values())
    return jank + penalty, dict(jank=jank, penalty=penalty, pen=pen, tput_ratio=T)
