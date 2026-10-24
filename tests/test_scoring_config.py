"""Offline checks for deterministic scoring configuration."""

import unittest
from unittest.mock import patch

from backend import scoring_config


class ScoringConfigTests(unittest.TestCase):
    """Verify valid constants and reject invalid configuration without fallback."""

    def test_default_config(self) -> None:
        """Accept the specified weights, versions and descending match bands."""
        self.assertEqual(scoring_config.validate_config(), {"ok": True, "value": True, "error": None})
        self.assertEqual(scoring_config.RUBRIC_VERSION_V2, "student-v2")
        self.assertEqual(scoring_config.EXTRACTOR_VERSION, "ext-1")
        self.assertEqual(scoring_config.COMPONENT_MAX, {
            "must_have_skills": 30, "experience_relevance": 20, "role_domain_fit": 15,
            "impact": 10, "education_fit": 10, "nice_to_have": 10, "logistics": 5,
        })
        self.assertEqual(scoring_config.BANDS, (
            (80, "Strong Match"), (65, "Good Match"), (45, "Partial Match"),
            (25, "Weak Match"), (0, "Not a Match"),
        ))

    def test_credit_and_policy_constants(self) -> None:
        """Preserve every specified evidence, recency and policy coefficient."""
        self.assertEqual(scoring_config.EVIDENCE_CREDIT, {
            "not_found": 0.0, "listed_only": 0.4,
            "used_in_project_or_role": 0.75, "used_with_outcome": 1.0,
        })
        self.assertEqual(scoring_config.RECENCY, {
            "within_2y": 1.0, "2_to_5y": 0.7, "over_5y": 0.4, "unknown": 0.85,
        })
        self.assertEqual(scoring_config.RELEVANCE_WEIGHT, {
            "high": 1.0, "medium": 0.6, "low": 0.2, "none": 0.0,
        })
        self.assertEqual(scoring_config.DOMAIN_RELATION, {
            "same": 1.0, "adjacent": 0.6, "unrelated": 0.2,
        })
        self.assertEqual(scoring_config.NEUTRAL_DEFAULT, 0.7)
        self.assertEqual(scoring_config.GATE_FAIL_CAP, 40)
        self.assertEqual(scoring_config.PENALTY_UNSUPPORTED_CLAIMS, 5)
        self.assertEqual(scoring_config.CONFIDENCE_REVIEW_THRESHOLD, 0.5)

    def test_invalid_maxima(self) -> None:
        """Reject wrong totals, empty mappings and invalid numeric maxima."""
        for maxima in (None, {}, {"skills": 99}, {"skills": 101},
                       {"skills": 100.0}, {"skills": True},
                       {"skills": 101, "other": -1}, {"skills": 100, "other": 0}):
            with self.subTest(maxima=maxima), patch.object(scoring_config, "COMPONENT_MAX", maxima):
                result = scoring_config.validate_config()
                self.assertIs(result.get("ok"), False)
                self.assertIsNone(result.get("value"))
                self.assertTrue(result.get("error"))

    def test_invalid_bands(self) -> None:
        """Reject ascending, duplicate, missing and malformed band definitions."""
        for bands in (None, (), ((0, "Low"), (80, "High")),
                      ((80, "High"), (80, "Duplicate"), (0, "Low")),
                      ((80, "High"),), ((80,),), (("80", "High"),),
                      ((True, "High"),), ((101, "High"),), ((-1, "Low"),),
                      ((0, ""),), ((0, None),)):
            with self.subTest(bands=bands), patch.object(scoring_config, "BANDS", bands):
                result = scoring_config.validate_config()
                self.assertIs(result.get("ok"), False)
                self.assertIsNone(result.get("value"))
                self.assertTrue(result.get("error"))
