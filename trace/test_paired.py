import io
import math
import unittest
import unittest.mock

import paired
import pilot


class PairedTest(unittest.TestCase):
    def test_student_critical_values(self):
        for df, expected in [(1, 12.7062047364), (5, 2.5705818356), (29, 2.0452296421), (100, 1.9839715184)]:
            self.assertAlmostEqual(paired.t95(df), expected, places=8)
        self.assertAlmostEqual(paired.t_cdf(2, 1), 0.5 + math.atan(2) / math.pi)

    def test_six_blocks_with_one_refresh_shift_have_wide_interval(self):
        mean, sd, lo, hi = paired.interval([0, 0, 0, 0, 0, 16.67])
        self.assertLess(lo, 0)
        self.assertGreater(hi, 9)
        self.assertGreater(sd, 0)

    def test_exact_sign_flip_resolution(self):
        self.assertEqual(paired.sign_flip([1] * 6), 2 / 64)
        self.assertEqual(paired.sign_flip([0] * 6), 1)

    def test_planning_uses_variability_and_precision(self):
        values = [-1, 0, 1]
        n = paired.plan_blocks(values, 1)
        self.assertEqual(n, 12)
        self.assertIsNone(paired.plan_blocks(values, 0.001))
        with self.assertRaises(ValueError):
            paired.plan_blocks([0, 0, 0], 1)

    def test_duplicate_blocks_and_mixed_stages_rejected(self):
        for rows in ('pilot,1,gain,1\npilot,1,gain,2\n',
                     'pilot,1,gain,1\nconfirmatory,2,gain,2\n',
                     'pilot,1,gain,1\npilot,2,cost,2\n'):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                paired.load(io.StringIO('stage,block,metric,value\n' + rows))


class PilotTest(unittest.TestCase):
    def test_order_and_references_come_from_registry(self):
        rows = pilot.plan(42)
        self.assertEqual(rows, pilot.plan(42))
        self.assertEqual(len(rows), 6)
        for block in range(1, 4):
            self.assertEqual({r['arm'] for r in rows if r['block'] == block},
                             {'fair/off/stock', 'fair/bg1/skip-cap'})
        self.assertTrue(all(r['mechanism_reference'] == 'fair/off/stock' for r in rows))
        self.assertTrue(all('amdgpu.user_queue=0' in r['cmdline'] for r in rows))
        fifo = pilot.plan(42, pair='fifo')
        self.assertEqual({r['arm'] for r in fifo}, {'fifo/off/stock', 'fifo/bg1/stock'})
        self.assertTrue(all(r['mechanism_reference'] == 'fifo/off/stock' for r in fifo))

    def test_sessions_include_every_extra_cell_repetition(self):
        rows = pilot.plan(7)
        reps = sum(r for *_, r in pilot.EXTRA_CELLS)
        orders = set()
        for row in rows:
            lines = row['commands'].splitlines()
            extra = [l for l in lines if 'workloads/' in l and '-pre --' not in l
                     and '-post --' not in l]
            self.assertEqual(len(extra), reps)
            names = [l.split()[2] for l in extra]
            self.assertEqual(len(set(names)), reps)
            self.assertTrue(all(n.startswith(row['matrix'] + '-') for n in names))
            self.assertTrue(lines[-1].split()[2].endswith('-post'))
            orders.add(tuple(n.split(row['matrix'])[1] for n in names))
        self.assertGreater(len(orders), 1)  # sessions are shuffled independently

    def test_status_points_at_first_unfinished_session(self):
        import csv as _csv, tempfile
        from pathlib import Path
        rows = pilot.plan(3)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'plan.csv'
            with path.open('w', newline='') as f:
                w = _csv.DictWriter(f, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
            with unittest.mock.patch.object(pilot, 'REPO', Path(d)):
                logs = Path(d) / 'traces/pilot-sessions'
                logs.mkdir(parents=True)
                (logs / f"{rows[0]['matrix']}.done").write_text('x')
                out = '\n'.join(pilot.status(path))
        first_todo = rows[1]
        entry = 'drmsched-' + first_todo['arm'].replace('/', '-') + '.efi'
        self.assertIn(f"--boot-loader-entry={entry}", out)
        self.assertIn(f"plan.csv {first_todo['block']} {first_todo['session']}", out)
        self.assertEqual(out.count('to do'), len(rows) - 1)

    def test_pacing_modes_and_missing_feedback(self):
        def rows(values):
            return [dict(frame=1, paced=1, latency=v) for v in values]
        self.assertEqual(pilot.pacing_mode(rows([33.4] * 20), 16.67), '2-refresh')
        self.assertEqual(pilot.pacing_mode(rows([50] * 20), 16.67), '3-refresh')
        self.assertEqual(pilot.pacing_mode(rows([33.4, 50] * 10), 16.67), 'mixed')
        self.assertEqual(pilot.pacing_mode(rows([33.4] * 3), 16.67), 'unknown')
