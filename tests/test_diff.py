"""Unit tests for the adjacent-version cycle diff classifier."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.diff import diff_cycles  # noqa: E402


def cyc(no, start, end, charge=1.0, discharge=1.0):
    return {
        "cycle_no": no,
        "started_at": start,
        "ended_at": end,
        "charge_ah": charge,
        "discharge_ah": discharge,
    }


class ExactSpanTests(unittest.TestCase):
    def test_identical_cycle_is_unchanged(self):
        d = diff_cycles([cyc(1, 0, 40)], [cyc(1, 0, 40)])
        self.assertEqual(d["summary"]["unchanged"], 1)
        self.assertEqual(d["summary"]["renumber_only"], 0)
        for key in ("added", "removed", "split", "merged", "capacity_changed"):
            self.assertEqual(d[key], [])

    def test_renumber_only_is_not_a_change(self):
        # identical span/capacity, just shifted cycle numbers
        d = diff_cycles(
            [cyc(1, 0, 40), cyc(2, 100, 140)],
            [cyc(5, 0, 40), cyc(6, 100, 140)],
        )
        self.assertEqual(d["summary"]["unchanged"], 2)
        self.assertEqual(d["summary"]["renumber_only"], 2)
        self.assertEqual(d["summary"]["new_total"], 2)

    def test_same_span_changed_capacity_is_capacity_change(self):
        d = diff_cycles(
            [cyc(1, 0, 40, charge=1.0)],
            [cyc(1, 0, 40, charge=1.5)],
        )
        self.assertEqual(len(d["capacity_changed"]), 1)
        chg = d["capacity_changed"][0]
        self.assertFalse(chg["span_changed"])
        self.assertAlmostEqual(chg["charge_delta_ah"], 0.5)
        self.assertAlmostEqual(chg["discharge_delta_ah"], 0.0)


class StructuralTests(unittest.TestCase):
    def test_one_old_cycle_split_into_two(self):
        old = [cyc(1, 0, 80)]
        new = [cyc(1, 0, 40), cyc(2, 40, 80)]
        d = diff_cycles(old, new)
        self.assertEqual(len(d["split"]), 1)
        self.assertEqual(d["split"][0]["from"]["started_at"], 0)
        self.assertEqual([c["ended_at"] for c in d["split"][0]["into"]], [40, 80])
        self.assertEqual(d["summary"]["split"], 1)
        self.assertEqual(d["added"], [])
        self.assertEqual(d["removed"], [])

    def test_two_old_cycles_merge_into_one(self):
        old = [cyc(1, 0, 40), cyc(2, 40, 80)]
        new = [cyc(1, 0, 80)]
        d = diff_cycles(old, new)
        self.assertEqual(len(d["merged"]), 1)
        self.assertEqual(
            [c["started_at"] for c in d["merged"][0]["from"]], [0, 40]
        )
        self.assertEqual(d["merged"][0]["into"]["ended_at"], 80)
        self.assertEqual(d["summary"]["merged"], 1)

    def test_added_and_removed_cycles(self):
        old = [cyc(1, 0, 40)]
        new = [cyc(1, 0, 40), cyc(2, 100, 140)]
        d = diff_cycles(old, new)
        self.assertEqual(len(d["added"]), 1)
        self.assertEqual(d["added"][0]["started_at"], 100)

        d2 = diff_cycles(new, old)
        self.assertEqual(len(d2["removed"]), 1)
        self.assertEqual(d2["removed"][0]["started_at"], 100)

    def test_span_changed_pair_is_capacity_change_with_flag(self):
        # overlapping but neither identical nor nested: treated as 1:1 via
        # containment because one span contains the other, spans differ
        old = [cyc(1, 0, 80, charge=2.0)]
        new = [cyc(1, 0, 60, charge=1.0)]
        d = diff_cycles(old, new)
        self.assertEqual(len(d["capacity_changed"]), 1)
        self.assertTrue(d["capacity_changed"][0]["span_changed"])

    def test_empty_baseline(self):
        new = [cyc(1, 0, 40)]
        d = diff_cycles([], new)
        self.assertEqual(d["added"], new)
        self.assertEqual(d["summary"]["old_total"], 0)
        self.assertEqual(d["summary"]["new_total"], 1)

    def test_both_empty(self):
        d = diff_cycles([], [])
        self.assertEqual(d["summary"]["new_total"], 0)
        self.assertEqual(d["unchanged"], [])

    def test_mixed_renumber_split_and_unaffected(self):
        # untouched cycle gets renumbered; another one splits
        old = [cyc(1, 0, 40), cyc(2, 100, 200)]
        new = [
            cyc(7, 0, 40),                  # renumber only
            cyc(8, 100, 150),               # split of old #2
            cyc(9, 150, 200),
        ]
        d = diff_cycles(old, new)
        self.assertEqual(d["summary"]["split"], 1)
        self.assertEqual(d["summary"]["unchanged"], 1)
        self.assertEqual(d["summary"]["renumber_only"], 1)
        self.assertEqual(d["added"], [])
        self.assertEqual(d["removed"], [])

    def test_adjacent_non_nested_cycles_do_not_pair(self):
        # touching at neither end, no containment -> removed + added
        old = [cyc(1, 0, 40)]
        new = [cyc(1, 100, 140)]
        d = diff_cycles(old, new)
        self.assertEqual(len(d["removed"]), 1)
        self.assertEqual(len(d["added"]), 1)
        self.assertEqual(d["capacity_changed"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
