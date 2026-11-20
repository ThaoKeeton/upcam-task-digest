"""The user's existing half-open report window and candidate selection."""

from datetime import datetime
import unittest

from report_contract import updated_selection, task_is_candidate


class McpCandidateWindowTests(unittest.TestCase):
    def test_candidate_lower_bound_is_report_start_not_previous_day(self):
        start = datetime.fromisoformat('2026-11-05T00:00:00+08:00')
        end = datetime.fromisoformat('2026-11-05T16:51:00+08:00')
        selection = updated_selection('2026-11-05', start, end)
        self.assertEqual(selection['candidate_updated_since'], start.isoformat())
        self.assertEqual(selection['candidate_overlap_seconds'], 0)
        self.assertEqual(selection['q'], "updated >= '2026-11-05T00:00:00+08:00'")
        self.assertFalse(task_is_candidate('2026-11-04T23:59:59+08:00', selection))
        self.assertTrue(task_is_candidate('2026-11-05T16:51:01+08:00', selection))
        self.assertEqual(selection['end_exclusive'], end.isoformat())

    def test_fractional_start_rounds_candidate_down_but_window_is_unchanged(self):
        start = datetime.fromisoformat('2026-11-05T00:00:00.123456+08:00')
        end = datetime.fromisoformat('2026-11-05T16:51:00+08:00')
        selection = updated_selection('2026-11-05', start, end)
        self.assertEqual(selection['candidate_updated_since'], '2026-11-05T00:00:00+08:00')
        self.assertEqual(selection['start'], start.isoformat())
        self.assertAlmostEqual(selection['candidate_overlap_seconds'], .123456)
        self.assertTrue(task_is_candidate('2026-11-05T00:00:00.123456+08:00', selection))


if __name__ == '__main__':
    unittest.main()
