import json
import sqlite3
import unittest
from datetime import date

from backend import deterministic_scoring, deterministic_store, store


class DeterministicStoreTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        deterministic_store.initialize(self.conn)
        self.addCleanup(self.conn.close)

    def test_scores_are_versioned_and_cached_idempotently(self):
        first = deterministic_store.content_hash("resume")
        second = deterministic_store.content_hash("job")
        result = {"score": 71, "components": {"must_have": 0.8}}
        self.assertIsNone(deterministic_store.get_score(self.conn, first, second, "v1"))
        deterministic_store.put_score(self.conn, first, second, "v1", result)
        self.assertEqual(deterministic_store.get_score(self.conn, first, second, "v1"), result)
        self.assertIsNone(deterministic_store.get_score(self.conn, first, second, "v2"))
        deterministic_store.put_score(self.conn, first, second, "v1", {"score": 73})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM deterministic_scores").fetchone()[0], 1)

    def test_jd_cache_and_unmatched_skill_counts(self):
        digest = deterministic_store.content_hash("job")
        self.assertIsNone(deterministic_store.get_jd_requirements(self.conn, digest))
        deterministic_store.put_jd_requirements(self.conn, digest, [{"skills": ["Python"]}])
        self.assertEqual(deterministic_store.get_jd_requirements(self.conn, digest), [{"skills": ["Python"]}])
        deterministic_store.note_unmatched(self.conn, ["Rust", "Rust"])
        deterministic_store.note_unmatched(self.conn, ["Rust"])
        self.assertEqual(self.conn.execute("SELECT count FROM unmatched_skills WHERE skill='Rust'").fetchone()[0], 2)
        self.assertEqual(deterministic_store.pending_alias_candidates(self.conn)[0]["skill"], "Rust")
        with self.assertRaises(ValueError):
            deterministic_store.pending_alias_candidates(self.conn, limit=0)

    def test_rejects_malformed_cache_values(self):
        with self.assertRaises(ValueError):
            deterministic_store.put_score(self.conn, "a", "b", "v1", {"score": True})
        with self.assertRaises(ValueError):
            deterministic_store.content_hash("")

    def test_dashboard_reads_v2_breakdown_without_display_only_weights(self):
        result = deterministic_scoring.score("Python project", "Python required", 0.7,
                                             as_of_date=date(2026, 1, 1))
        result["scorer_version"] = "deterministic-v2"
        result.pop("weights")
        displayed = store._deterministic_breakdown(json.dumps(result))
        self.assertEqual(displayed["weights"], deterministic_scoring.WEIGHTS)

    def test_only_valid_llm_explanations_are_cached(self):
        explanation = {"explanation": "Partial evidence-based fit.", "gap_summary": "GraphQL is missing.",
                       "explanation_source": "llm"}
        self.assertIsNone(deterministic_store.get_explanation(self.conn, "r", "j", "v1"))
        deterministic_store.put_explanation(self.conn, "r", "j", "v1", explanation)
        self.assertEqual(deterministic_store.get_explanation(self.conn, "r", "j", "v1"), explanation)
        with self.assertRaises(ValueError):
            deterministic_store.put_explanation(self.conn, "r", "j", "v1", {"explanation_source": "template"})


if __name__ == "__main__":
    unittest.main()
