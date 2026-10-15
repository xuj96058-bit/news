"""Budget allocation for slow reasoning without sacrificing the first review."""
import unittest

from src.research_timing import STAGE_SECONDS, publication_reserves, stage_deadline


class ResearchTimingTests(unittest.TestCase):
    def test_one_hour_budget_protects_full_publishing_stages(self):
        reserve = publication_reserves(3600)
        self.assertEqual(reserve, {"planning": 360, "writing": 720, "reviewing": 900, "auditing": 720})
        self.assertEqual(3600 - sum(reserve.values()), 900)

    def test_short_budget_scales_reserves_without_extending_it(self):
        reserve = publication_reserves(120)
        self.assertAlmostEqual(sum(reserve.values()), 90)
        self.assertTrue(all(value > 0 for value in reserve.values()))
        self.assertEqual(stage_deadline("organizing", 120, now=0, phase_deadline=30), 30)

    def test_source_extraction_and_writing_allow_long_reasoning(self):
        self.assertEqual(stage_deadline("organizing", 3600, now=100), 580)
        self.assertEqual(stage_deadline("writing", 3600, now=100), 820)
        self.assertEqual(stage_deadline("reviewing", 3600, now=100), 1000)
        self.assertEqual(stage_deadline("auditing", 3600, now=1575), 2295)

    def test_deadlines_protect_future_stages(self):
        self.assertEqual(stage_deadline("writing", 3600, now=1800, reserve_seconds=1620), 1980)
        self.assertEqual(stage_deadline("reviewing", 3600, now=2600, reserve_seconds=720), 2880)
        self.assertEqual(stage_deadline("auditing", 3600, now=3500), 3600)

    def test_legacy_reserves_have_no_unused_planning_allowance(self):
        reserve = publication_reserves(3600, "legacy")
        self.assertNotIn("planning", reserve)
        self.assertEqual(reserve["reviewing"], STAGE_SECONDS["reviewing"])

    def test_invalid_total_budget_is_rejected(self):
        for value in (True, 0, -1, float("inf"), float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                publication_reserves(value)


if __name__ == "__main__":
    unittest.main()
