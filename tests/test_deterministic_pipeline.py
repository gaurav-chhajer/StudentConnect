import json
import sqlite3
import unittest
from unittest.mock import patch

from backend import pipeline_skeleton


class DeterministicPipelineTests(unittest.TestCase):
    def test_redundant_unknown_and_implied_fuzzy_proposals_are_filtered(self):
        from backend import deterministic_scoring
        pairs = [dict(jd_skill="Scrum", resume_skill="SCRUM", cosine=1.0, type="fuzzy"),
                 dict(jd_skill="Python", resume_skill="Flask", cosine=.95, type="fuzzy")]
        self.assertEqual(deterministic_scoring.pending_fuzzy_matches(
            "Skills\nSCRUM, Flask", "Requirements\nScrum\nPython", pairs), [])
        invalid = [dict(pairs[0], cosine=.5)]
        self.assertEqual(deterministic_scoring.pending_fuzzy_matches(
            "Skills\nSCRUM", "Requirements\nScrum", invalid), invalid)

    def test_local_score_adapts_and_caches_without_changing_score_contract(self):
        scorer = type("Scorer", (), {})()
        scorer.conn = sqlite3.connect(":memory:")
        self.addCleanup(scorer.conn.close)
        user = {"resume_text": "Skills\nPython\n", "name": "Candidate"}
        job = {"title": "Software Engineer", "department": None, "location": "Remote",
               "raw_json": json.dumps({"content": "Requirements\nPython required\n"})}
        with patch("backend.career_sources.prepare_job", side_effect=lambda value: dict(value)), \
                patch("backend.semantic_similarity.section_similarity", return_value=0.72) as semantic, \
                patch("backend.semantic_similarity.fuzzy_skill_matches", return_value=[]):
            result = pipeline_skeleton.score_job_deterministic(user, job, "Example", scorer)
            repeated = pipeline_skeleton.score_job_deterministic(user, job, "Example", scorer)
        self.assertEqual(result, repeated)
        self.assertEqual(result["fit_score"], 56)
        self.assertIn("Python", result["matched_skills"])
        self.assertEqual(result["missing_requirements"], [])
        self.assertEqual(result["scorer_version"], "deterministic-v4")
        semantic.assert_called_once()


if __name__ == "__main__":
    unittest.main()
