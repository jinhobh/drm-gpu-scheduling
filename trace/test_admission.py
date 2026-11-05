"""Adversarial checks for bg1 trace validation; run via unittest discover."""
import unittest

import admission


def event(serial=1, action=0, count=1, fence="10:1", priority=2, credits=1, ring="gfx_0.0.0"):
    result = (f"worker-42 [001] 1.000: drm_sched_admission: dev=0000:07:00.0 "
            f"ring={ring} fence={fence} client_id=8 serial={serial} action={action} "
            f"priority={priority} count={count} credits={credits} limit=2 error=0\n")
    if action == 0:
        result += (f"worker-42 [001] 1.000: drm_sched_admission_dispatch: dev=0000:07:00.0 "
                   f"ring={ring} fence={fence} reservation={serial} bg_limit=1 "
                   f"priority={priority} credits={credits} limit=2\n")
    return result


def check(blocks, **kwargs):
    return admission.validate((line for block in blocks for line in block.splitlines()), **kwargs)


class AdmissionTest(unittest.TestCase):
    def test_cross_cpu_report_order_is_not_transition_order(self):
        rows = check([event(2, 1, 0), event()], require_drained=True)
        self.assertEqual((rows[0]["transitions"], rows[0]["initial"], rows[0]["final"]), (2, 0, 0))
        self.assertEqual(rows[0]["dispatches"], 1)

    def test_capture_boundaries_are_reported(self):
        rows = check([event(10, 1, 0), event(11, 0, 1, "10:2")])
        self.assertEqual((rows[0]["initial"], rows[0]["final"]), (1, 1))
        with self.assertRaisesRegex(ValueError, "boundary"):
            check([event()], require_drained=True)

    def test_missing_duplicate_and_lost_events_rejected(self):
        for events in ([event(), event(3, 1, 0)], [event(), event()],
                       [event(), "CPU: 0 [LOST 10 EVENTS]"]):
            with self.subTest(events=events), self.assertRaises(ValueError):
                check(events)

    def test_occupancy_and_original_credit_overflow_rejected(self):
        for events in ([event(), event(2, 0, 2, "10:2")],
                       [event(credits=3)], [event(count=0)]):
            with self.subTest(events=events), self.assertRaises(ValueError):
                check(events)

    def test_priority_exemption_and_release_identity(self):
        for events in ([event(priority=1)], [event(), event(2, 1, 0, "99:9")],
                       [event(), event(2, 1, 0), event(3, 1, 0)]):
            with self.subTest(events=events), self.assertRaises(ValueError):
                check(events)

    def test_cleanup_releases_once(self):
        rows = check([event(), event(2, 2, 0)], require_drained=True)
        self.assertEqual(rows[0]["final"], 0)

    def test_background_dispatch_requires_matching_reservation(self):
        extra = ("drm_sched_admission_dispatch: dev=0000:07:00.0 ring=gfx_0.0.0 "
                 "fence=20:1 reservation=0 bg_limit=1 priority=2 credits=1 limit=2")
        for row in (extra, extra.replace("reservation=0", "reservation=1"),
                    extra.replace("priority=2", "priority=1").replace("reservation=0", "reservation=1")):
            with self.subTest(row=row), self.assertRaises(ValueError):
                check([event(), row])

    def test_dispatch_on_other_scheduler_cannot_escape_validation(self):
        row = ("drm_sched_admission_dispatch: dev=0000:07:00.0 ring=gfx_1.0.0 "
               "fence=20:1 reservation=0 bg_limit=1 priority=2 credits=1 limit=2")
        with self.assertRaisesRegex(ValueError, "reservation history"):
            check([event(), row])

    def test_boundary_dispatch_identity_is_checked(self):
        prefix = ("drm_sched_admission_dispatch: dev=0000:07:00.0 ring=gfx_0.0.0 "
                  "fence=10:1 reservation=9 bg_limit=1 priority=2 credits=1 limit=2")
        rows = check([event(10, 1, 0), prefix])
        self.assertEqual(rows[0]["initial"], 1)
        with self.assertRaisesRegex(ValueError, "boundary reservation"):
            check([event(10, 1, 0), prefix.replace("fence=10:1", "fence=20:1")])

    def test_filtered_transition_only_trace_is_not_a_pass(self):
        with self.assertRaisesRegex(ValueError, "dispatch events"):
            check([event().splitlines()[0]])

    def test_schedulers_have_independent_serials(self):
        rows = check([event(), event(ring="gfx_1.0.0")])
        self.assertEqual(len(rows), 2)

    def test_empty_or_malformed_capture_is_not_a_pass(self):
        for lines in ([], ["normal event"], ["drm_sched_admission: dev=x ring=gfx"]):
            with self.subTest(lines=lines), self.assertRaises(ValueError):
                check(lines)

    def test_scan_summary_and_invalid_selection(self):
        scan = ("drm_sched_admission_select: dev=0000:07:00.0 ring=gfx_0.0.0 "
                "examined=3 skipped=2 reason=0 priority=1 duration_ns=1500")
        row = check([event(), scan])[0]
        self.assertEqual((row["skipped"], row["max_examined"], row["max_select_ns"]), (2, 3, 1500))
        for bad in (scan.replace("skipped=2", "skipped=4"),
                    scan.replace("priority=1", "priority=-1")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                check([event(), bad])


if __name__ == "__main__":
    unittest.main()
