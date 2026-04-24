from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import unittest

from src.diff_engine import compute_mirror_diff, safety_check


class TestMirrorDiff(unittest.TestCase):
    def test_pure_add(self):
        diff = compute_mirror_diff({1, 2, 3}, set())
        self.assertEqual(diff["to_add"], {1, 2, 3})
        self.assertEqual(diff["to_remove"], set())

    def test_pure_remove(self):
        diff = compute_mirror_diff(set(), {1, 2})
        self.assertEqual(diff["to_add"], set())
        self.assertEqual(diff["to_remove"], {1, 2})

    def test_noop(self):
        diff = compute_mirror_diff({1, 2}, {1, 2})
        self.assertEqual(diff["to_add"], set())
        self.assertEqual(diff["to_remove"], set())

    def test_mixed(self):
        diff = compute_mirror_diff({1, 2, 4}, {2, 3})
        self.assertEqual(diff["to_add"], {1, 4})
        self.assertEqual(diff["to_remove"], {3})

    def test_iterables_not_just_sets(self):
        diff = compute_mirror_diff([1, 1, 2], (2, 3))
        self.assertEqual(diff["to_add"], {1})
        self.assertEqual(diff["to_remove"], {3})


class TestSafetyCheck(unittest.TestCase):
    def test_first_sync_bypass(self):
        diff = {"to_add": {1, 2}, "to_remove": set()}
        ok, _ = safety_check(diff, current_count=0, threshold=0.2)
        self.assertTrue(ok)

    def test_first_sync_bypass_even_with_removes(self):
        # current_count==0 means there's nothing to lose; plan says pass-through.
        diff = {"to_add": set(), "to_remove": {1, 2, 3}}
        ok, _ = safety_check(diff, current_count=0, threshold=0.2)
        self.assertTrue(ok)

    def test_under_threshold(self):
        diff = {"to_add": set(), "to_remove": {1}}
        ok, _ = safety_check(diff, current_count=10, threshold=0.2)
        self.assertTrue(ok)

    def test_exactly_at_threshold_passes(self):
        # 2/10 = 0.20 == threshold → strictly greater is abort, equal passes.
        diff = {"to_add": set(), "to_remove": {1, 2}}
        ok, _ = safety_check(diff, current_count=10, threshold=0.2)
        self.assertTrue(ok)

    def test_over_threshold_aborts(self):
        diff = {"to_add": set(), "to_remove": {1, 2, 3}}
        ok, reason = safety_check(diff, current_count=10, threshold=0.2)
        self.assertFalse(ok)
        self.assertIn("threshold", reason.lower())

    def test_tight_threshold_single_delete_aborts(self):
        # Plan's own verification scenario: threshold=0.01, remove 1/10.
        diff = {"to_add": set(), "to_remove": {1}}
        ok, _ = safety_check(diff, current_count=10, threshold=0.01)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
