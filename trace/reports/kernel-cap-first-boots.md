# First hardware boots of the gfx admission cap (pilot evidence)

2026-11-04. First boots of `linux-drmsched` (7.2.11-arch1-1-drmsched) on the
Radeon 860M laptop. One run per new arm: **feasibility/pilot evidence, not a
confirmatory result.** Arms as defined in [arms.csv](../kernel/arms.csv).

## Setup and safety

- Package built from the pinned recipe (Rust 1.99.0 -> 1.99.1 config pin only).
  Installed beside stock; six per-arm UKIs via a dedicated mkinitcpio preset;
  stock `arch-linux.efi` pinned as the systemd-boot default (the drmsched entries
  sort above it). Stock UKI/vmlinuz hashes verified unchanged after install.
- Every boot was a one-shot entry. Both captures report identical
  `gpu-sched.ko.zst` (`0142355a…`) and `amdgpu.ko.zst` (`463362ed…`) hashes:
  one binary, arms differ only by command line. Hyprland 0.56.2 (`efb5099`).
- `fifo/off/stock` boot: parameters as specified; no kernel warning absent from
  the previous stock boot. `fifo/bg1/stock` and `fair/bg1/skip-cap` boots: no
  amdgpu ring timeout, reset or fault. The single kernel WARNING
  (`power_supply_unregister`) also occurs on the stock kernel.
- `admission.py` passed on both captures: NORMAL/LOW occupancy never above one,
  initial = final = 0, no serial gaps or unmatched transitions. Selection cost:
  max 7.0 us (FIFO, 1 node examined) and 3.2 us (fair, up to 4 examined).
- Not yet done: 30-minute soak, three suspend/resume cycles, recovery test,
  throughput cost, tiny-job flood, self-cost cells.

## Runs

| Run | Arm | Workload |
|---|---|---|
| `traces/bg1-check/20261104-214103` | fifo/bg1/stock | `burst.sh vk-hog --iters 736 -b 20` (**default 30-frame bursts, 500 ms idle; not the m3 settings**) |
| `traces/fair-bg1-check/20261104-214721` | fair/bg1/skip-cap | same as below; **invalid**: hog exited at warm-up after `timestamp probes disagree` (also seen once in 146 earlier stock runs) |
| `traces/fair-bg1-check/20261104-214921` | fair/bg1/skip-cap | `burst.sh vk-hog --iters 736 -n 10 -i 300 -b 20` (m3 settings) |

In the valid fair run the victim's live timestamp correction was loose
(-2.50 ms +- 2.37 ms), so its `burst_stats.py` start latencies are unreliable by
a few ms. All comparisons below use kernel-side times instead.

## Result: kernel wait until GPU start

`python3 trace/start_wait.py RUN... --ahead` (sched_wait + ring_wait from
`parse.py`'s jobs.csv; comparable whether a job waits in the scheduler or ring).
Historical m3 rows ran on the stock kernel with older userspace: context only.

| Condition | Victim p50 / p99 (ms) | Hyprland HIGH p50 / p99 (ms) |
|---|---|---|
| m3 FIFO, vk-hog (m3-p1r r1–r3) | 13.2–15.4 / 16.4–18.0 | 9.8–13.0 / 14.7–16.4 |
| m3 fair, vk-hog (m3-p2 r1–r3) | 12.1–14.8 / 15.1–17.7 | 10.0–12.7 / 14.2–16.6 |
| m3 cooperating vk-hog1 (m3-p1r, m3-p2, 6 runs) | 6.2–6.5 / 9.1–9.8 | 3.6–3.9 / 8.2–8.5 |
| **fifo/bg1/stock** | 12.74 / 17.28 | **3.67 / 8.12** |
| **fair/bg1/skip-cap** | **5.31 / 8.66** | **3.35 / 8.09** |

Hog job exec was ~8.1–8.2 ms in both new runs (7.8–7.9 ms in m3-p1r).

### FIFO: the cap protects the compositor, not a NORMAL victim

In 697/700 victim jobs exactly one hog job was dispatched while the victim was
ready, and in every such case that hog job had been **queued before** the victim.
The cap moves the hog's second job from the ring into the scheduler queue, but
FIFO orders entities by their head job's submit time, so the older hog job still
goes first. The victim waits for the running hog job plus one more, as without
the cap. `vk-hog1` helped because the cooperating hog does not submit its next job
until the previous one finishes, so the victim is the oldest waiting job.

HIGH-priority Hyprland is exempt and its run queue is checked first. With the hog
held to one dispatched job, a credit is free for Hyprland, which waits only for the
remainder of the running hog job: ~3.7 ms p50 instead of ~10–13 ms.

**Consequence:** the protocol's FIFO off/bg1 primary contrast cannot show the
intended NORMAL-victim GPU-start benefit; it is mechanistically excluded, not just
underpowered.

### Fair: the cap reproduces the cooperating-hog benefit

In 297/300 victim jobs **no** hog job was dispatched ahead of the ready victim.
Fair selects the least-serviced entity, so once the running hog job completes the
victim goes next. Victim wait matches or slightly beats the m3 `vk-hog1` range,
without any client change. Hyprland gets the same compositor benefit as under FIFO.

Skip-cap selection stepped past a cap-blocked head **2,436 times** in this run.
In `fair/bg1/stock` each of those would leave the ready HIGH entity behind it
waiting, which is why that arm is a diagnostic and was not booted first.

## Limits

One run per arm, one boot each, fresh boots, no same-boot uncapped control
(mechanism reference must be the same-policy cap-off `vk-hog1` in a paired boot),
different burst settings for the FIFO run, a loose victim timestamp correction in
the fair run, and no throughput or self-cost data. The direction of both findings
follows from scheduler ordering rules and the per-job counts above, but the
magnitudes need the paired pilot.
