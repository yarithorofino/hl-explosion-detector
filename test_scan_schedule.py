#!/usr/bin/env python3
"""Schedule helpers only. No network and no webhook."""
import unittest
from datetime import datetime, timezone

import scan_schedule as sched


def epoch(iso):
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp()


class ScheduleTests(unittest.TestCase):
    def test_next_slot_lands_on_the_following_quarter_plus_offset(self):
        # 03:33:00 UTC -> 03:45:20
        now = epoch("2026-11-10T03:33:00")
        target = sched.next_slot_epoch(now, offset=20, period=900)
        self.assertEqual(datetime.fromtimestamp(target, timezone.utc).strftime("%H:%M:%S"), "03:45:20")

    def test_just_after_the_offset_waits_for_the_next_quarter(self):
        now = epoch("2026-11-10T03:45:25")
        target = sched.next_slot_epoch(now, offset=20, period=900)
        self.assertEqual(datetime.fromtimestamp(target, timezone.utc).strftime("%H:%M:%S"), "04:00:20")

    def test_exactly_on_a_quarter_waits_for_the_candle_offset(self):
        now = epoch("2026-11-10T03:45:00")
        target = sched.next_slot_epoch(now, offset=20, period=900)
        self.assertEqual(int(target - now), 20)

    def test_five_scans_from_a_throttled_start_include_the_45(self):
        # Production starts landed near :52–:55 and, with only 4 passes,
        # finished on :30 and left :45 unwatched.
        for stamp, fifth in (
            ("2026-11-10T08:55:00", "09:45:20"),
            ("2026-11-10T09:55:00", "10:45:20"),
            ("2026-11-10T10:52:00", "11:45:20"),
        ):
            start = epoch(stamp)
            starts = sched.plan_scan_starts(start, scan_seconds=70, iterations=5)
            labels = [datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M:%S") for t in starts]
            self.assertEqual(len(labels), 5, labels)
            self.assertTrue(labels[-1].endswith("45:20"), labels)
            self.assertEqual(labels[-1], fifth)
            self.assertLess(starts[-1] - start, 3600)

    def test_no_new_scan_starts_after_sixty_minutes(self):
        start = epoch("2026-11-10T03:33:00")
        # A very slow scan still must not open a pass past the hour mark.
        starts = sched.plan_scan_starts(start, scan_seconds=25 * 60, iterations=5)
        self.assertEqual(len(starts), 3)
        self.assertLess(starts[-1] - start, 3600)
        self.assertFalse(sched.should_start_iteration(3600, index=2))
        self.assertTrue(sched.should_start_iteration(3600, index=1))

    def test_webhook_test_is_a_single_iteration(self):
        self.assertEqual(sched.iterations_for(True, 5), 1)
        self.assertEqual(sched.iterations_for(True, 4), 1)
        self.assertEqual(sched.iterations_for(False, None), 5)
        self.assertEqual(sched.iterations_for(False, "2"), 2)
        self.assertEqual(sched.iterations_for(False, "99"), 8)
        self.assertEqual(sched.iterations_for(False, "0"), 1)
        self.assertEqual(sched.iterations_for(False, "nope"), 5)

    def test_newer_queued_run_is_noticed_and_the_current_one_is_not(self):
        runs = [
            {"id": 10, "status": "in_progress"},
            {"id": 11, "status": "queued"},
            {"id": 9, "status": "queued"},
        ]
        self.assertTrue(sched.newer_run_waiting(runs, 10))
        self.assertFalse(sched.newer_run_waiting(runs, 11))
        self.assertFalse(sched.newer_run_waiting(
            [{"id": 12, "status": "completed"}], 10
        ))
        self.assertFalse(sched.newer_run_waiting(None, 10))


if __name__ == "__main__":
    unittest.main()
