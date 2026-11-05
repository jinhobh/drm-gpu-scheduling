# BORE-inspired GPU Scheduler for Linux Kernel

**70% lower graphic latency:** a kernel scheduler change so apps and the desktop stay responsive while a game or another heavy program is using the GPU.

| Wait before the GPU starts a frame | Stock Linux 7.2 | This change | Improvement |
|---|---:|---:|---:|
| App's frame (launch, animation, scroll) | 13–15 ms | 5.3–6.2 ms | **~60% less, ~8.5 ms faster** |
| App's frame, worst 1% | 16–18 ms | 8.7 ms | **~50% less** |
| Desktop compositor (whole screen) | 10–13 ms | 3.4–3.8 ms | **~70% less, ~8 ms faster** |
| Heavy program's speed | 118 jobs/s | ~117 jobs/s | ~1% slower (early hint) |

At 60 Hz the screen refreshes every 16.7 ms, so a 14 ms wait costs almost a whole frame.

## Before: your frame waits behind two heavy jobs

```
GPU  ┃▓▓▓▓ heavy job ▓▓▓▓┃▓▓▓▓ heavy job ▓▓▓▓┃░ app ░┃
              ▲                                ▲
       app frame ready ─────── waits ~14 ms ───┘
```

Stock Linux lets each program keep **2 jobs** queued on the GPU. A heavy job takes about 8 ms and can't be interrupted.

## After: one job at a time, and the least-served program goes next

```
GPU  ┃▓▓▓▓ heavy job ▓▓▓▓┃░ app ░┃▓▓▓▓ heavy job ▓▓▓▓┃
              ▲           ▲
       app frame ready ───┘ waits ~5 ms
```

```
                 ┌──────────────────────────┐
 heavy program ──┤ 1. cap: 1 job in flight  ├──┐
                 └──────────────────────────┘  │   ┌────────────────────────────┐
                                               ├──▶│ 2. fair pick: least GPU    │──▶ GPU
 your app ─────────────────────────────────────┤   │    time used goes first    │
                                               │   └────────────────────────────┘
 compositor (HIGH) ── exempt from cap ─────────┘
```

Each change alone is not enough:

| | App frame | Desktop |
|---|---:|---:|
| Stock | 13–15 ms | 10–13 ms |
| Cap only | 12.7 ms ✗ | 3.7 ms ✓ |
| Fair ordering only | 12–15 ms ✗ | 10–13 ms ✗ |
| **Cap + fair** | **5.3–6.2 ms ✓** | **3.4–3.8 ms ✓** |

---

<sub>Early results: one Radeon 860M laptop, one boot per configuration, with the stock numbers taken from earlier sessions. Details, status and next steps are in [plan.md](plan.md). The kernel patch and experiment setup are in [trace/kernel/](trace/kernel/README.md).</sub>
