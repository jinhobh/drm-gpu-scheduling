"""Synthetic checks for output selection and timing; run with unittest discover."""
import unittest
from unittest.mock import patch

import pacing


class PacingTest(unittest.TestCase):
    def analyze(self, outputs):
        frame = dict(burst=0, frame=1, paced=1, state="presented", msc=12,
                     t_intended=20_000_000, t_submit=21_000_000,
                     t_present=22_000_000, t_present_ret=23_000_000,
                     presented=50_000_000)
        with patch.object(pacing.burst_stats, "load", return_value=([], [frame])), \
             patch.object(pacing, "events", return_value=([19e6, 35e6], outputs)):
            return pacing.analyze("synthetic")

    def test_selects_output_by_sequence_and_time(self):
        rows = self.analyze({0: [(10_000_000, 10), (50_000_000, 12)],
                             1: [(10_000_000, 10), (70_000_000, 12)]})
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["crtc"], r["outputs"]), (0, 2))
        self.assertEqual(r["commit_to_next_hypr"], 13)
        self.assertEqual(r["next_hypr_to_present"], 15)
        self.assertEqual(r["commit_to_present"], 28)
        self.assertEqual(r["hypr_between"], 0)

    def test_missing_output_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "no output matches"):
            self.analyze({0: [(50_000_000, 99)]})

    def test_ambiguous_output_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.analyze({0: [(50_000_000, 12)], 1: [(50_000_000, 12)]})


if __name__ == "__main__":
    unittest.main()
