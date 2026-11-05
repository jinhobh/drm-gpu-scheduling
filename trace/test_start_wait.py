"""Synthetic checks for start_wait.py; run with unittest discover."""
import csv
import tempfile
import unittest

import start_wait

JOB_COLS = ["ctx", "seq", "ring", "client", "comm", "prio", "t_queue", "dep_wait",
            "order_wait", "sched_wait", "ring_wait", "exec"]
FRAME_COLS = ["burst", "frame", "n_jobs", "ctx", "seq", "start_lat", "ok"]


def job(ctx, seq, client, comm, prio, t_queue, sched, ring=0.0, exec_ms=1.0):
    return dict(ctx=ctx, seq=seq, ring="gfx_0.0.0", client=client, comm=comm, prio=prio,
                t_queue=t_queue, dep_wait=0, order_wait=0, sched_wait=sched,
                ring_wait=ring, exec=exec_ms)


class StartWaitTest(unittest.TestCase):
    def run_dir(self, jobs, frames=()):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = tmp.name
        for name, cols, rows in (("jobs.csv", JOB_COLS, jobs), ("frames.csv", FRAME_COLS, frames)):
            with open(f"{d}/{name}", "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=cols)
                w.writeheader()
                w.writerows(rows)
        return d

    def jobs(self):
        hog = [job(1, i, 10, "burst_client", 2, 1.000 + i * 0.008, 8.0, exec_ms=8.0)
               for i in range(150)]
        victim = [job(2, i, 11, "burst_client", 2, 1.0045 + i * 0.008, 3.0 + i % 2, ring=0.5)
                  for i in range(150)]
        hypr = [job(3, i, 12, "Hyprland", 1, 1.002 + i * 0.016, 1.0) for i in range(5)]
        return hog + victim + hypr

    def test_identifies_roles_and_combines_sched_and_ring_wait(self):
        hog, victim, hypr = start_wait.load(self.run_dir(self.jobs()))
        self.assertEqual({r["client"] for r in hog}, {"10"})
        self.assertEqual({r["client"] for r in victim}, {"11"})
        self.assertEqual(len(hypr), 5)
        self.assertEqual(start_wait.wait(victim[0]), 3.5)

    def test_ahead_counts_older_hog_jobs(self):
        hog = [job(1, 0, 10, "burst_client", 2, 1.000, 0.0, exec_ms=8),
               job(1, 1, 10, "burst_client", 2, 1.001, 5.0, exec_ms=8)]
        victim = [job(2, 0, 11, "burst_client", 2, 1.002, 10.0)]
        total, older = start_wait.ahead(hog, victim)
        self.assertEqual(total, {1: 1})
        self.assertEqual(older, {1: 1})

    def test_frame0_matches_frames_by_fence_and_skips_invalid(self):
        frames = [dict(burst=0, frame=0, n_jobs=1, ctx=2, seq=0, start_lat=3.6, ok=1),
                  dict(burst=0, frame=1, n_jobs=1, ctx=2, seq=1, start_lat=4.6, ok=1),
                  dict(burst=1, frame=0, n_jobs=2, ctx=2, seq=2, start_lat=3.7, ok=1),
                  dict(burst=2, frame=0, n_jobs=1, ctx=2, seq=3, start_lat=9.9, ok=0),
                  dict(burst=3, frame=0, n_jobs=1, ctx=9, seq=9, start_lat=1.0, ok=1)]
        kernel, app, later, multi, missing = start_wait.frame0(self.run_dir(self.jobs(), frames))
        self.assertEqual(kernel, [3.5, 3.5])
        self.assertEqual(app, [3.6, 3.7])
        self.assertEqual(later, [4.5])
        self.assertEqual((multi, missing), (1, 1))

    def test_frame0_requires_matched_frames(self):
        with self.assertRaisesRegex(ValueError, "no matched frame-0"):
            start_wait.frame0(self.run_dir(self.jobs()))


if __name__ == "__main__":
    unittest.main()
