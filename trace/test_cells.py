"""Synthetic checks for selfcost.py, throughput.py and join.kernel_span."""
import csv
import os
import tempfile
import types
import unittest

import join
import selfcost
import throughput

BURST_COLS = ["burst", "frame", "idle_ms", "ext_w", "ext_h", "msc", "presented", "state", "jobs"]
FRAME_COLS = ["burst", "frame", "n_jobs", "ok", "pick", "credit", "ring_wait", "span_trace",
              "exec_trace_sum", "between_trace"]


def write(path, cols, rows, header=()):
    with open(path, "w", newline="") as f:
        for h in header:
            f.write(h + "\n")
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


class CellsTest(unittest.TestCase):
    def tmp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        return t.name

    def test_kernel_span_orders_jobs_and_sums_gaps(self):
        js = [types.SimpleNamespace(t_queue=1.000, t_hw=1.0020, t_done=1.0030),
              types.SimpleNamespace(t_queue=1.0001, t_hw=1.0050, t_done=1.0060),
              types.SimpleNamespace(t_queue=1.0002, t_hw=1.0060, t_done=1.0070)]
        k = join.kernel_span(list(reversed(js)))
        self.assertAlmostEqual(k["span_trace"], 7.0)
        self.assertAlmostEqual(k["exec_trace_sum"], 3.0)
        self.assertAlmostEqual(k["between_trace"], 2.0)

    def test_selfcost_drops_unsettled_frames_and_reports_presentation(self):
        d = self.tmp()
        bursts, frames = [], []
        for b in range(2):
            for f in range(3):
                ext = (1920, 1200) if (b, f) == (0, 0) else (948, 1167)
                bursts.append(dict(burst=b, frame=f, idle_ms=500, ext_w=ext[0], ext_h=ext[1],
                                   msc=10 * b + 2 * f, presented=1_000_000_000 * b + 33_000_000 * f,
                                   state="presented", jobs=3))
                frames.append(dict(burst=b, frame=f, n_jobs=3, ok=1, pick=0.5, credit=1.0,
                                   ring_wait=0.25, span_trace=4 + f, exec_trace_sum=1.0,
                                   between_trace=0.2))
        write(f"{d}/burst-a.csv", BURST_COLS, bursts, header=["# burst_client pid=1"])
        write(f"{d}/frames-burst-a.csv", FRAME_COLS, frames)
        self.assertEqual(selfcost.logs(d), ["burst-a.csv"])
        c = selfcost.client(d, "burst-a.csv")
        self.assertEqual((c["frames"], c["drop"], c["matched"]), (5, 1, 5))
        self.assertEqual(c["span"], 5)  # spans 5, 6, 4, 5, 6
        self.assertEqual(c["f0_wait"], 1.75)  # only burst 1's frame 0 kept
        self.assertEqual(c["jobs"], {3: 5})
        self.assertEqual(c["skip"], 3 / 5)  # msc steps of 2: one skipped vblank per pair
        self.assertAlmostEqual(c["fps"], 1 / 0.033, places=3)

    def test_throughput_uses_steady_window(self):
        d = self.tmp()
        rows = [dict(ring="gfx_0.0.0", comm="vkmark", client=5, prio=2, t_queue=10 + i / 100,
                     exec=0.05) for i in range(1000)]
        rows += [dict(ring="gfx_0.0.0", comm="Hyprland", client=6, prio=1, t_queue=10 + i,
                      exec=0.5) for i in range(10)]
        write(f"{d}/jobs.csv", ["ring", "comm", "client", "prio", "t_queue", "exec"], rows)
        out = throughput.rates(d, skip=3, tail=1, min_jobs=200)
        self.assertEqual([c["comm"] for c in out], ["vkmark"])
        self.assertAlmostEqual(out[0]["rate"], 100, delta=1)
        self.assertAlmostEqual(out[0]["busy"], 0.005, delta=1e-4)


if __name__ == "__main__":
    unittest.main()
