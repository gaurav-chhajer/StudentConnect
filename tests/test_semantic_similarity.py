import unittest

from backend import semantic_similarity as semantic


class FakeScorer:
    def __init__(self, vectors):
        self.vectors = vectors
        self.calls = []

    def embedding(self, text, owner):
        self.calls.append((text, owner))
        return self.vectors[text], []


def vector(first, second=0.0):
    return [first, second] + [0.0] * 382


class SemanticSimilarityTests(unittest.TestCase):
    def test_sections_compare_each_jd_section_to_best_resume_section(self):
        scorer = FakeScorer({
            "Python role": vector(1), "SQL project": vector(0, 1),
            "Python required": vector(1), "SQL preferred": vector(0, 1),
        })
        score = semantic.section_similarity(
            "Experience\nPython role\nProjects\nSQL project",
            "Requirements\nPython required\nNice to have\nSQL preferred",
            scorer, resume_owner="resume:1", jd_owner="jd:hash",
        )
        self.assertEqual(score, 1.0)
        self.assertEqual(len(scorer.calls), 4)

    def test_section_similarity_rejects_invalid_owner_or_empty_document(self):
        scorer = FakeScorer({"text": vector(1)})
        with self.assertRaises(ValueError):
            semantic.section_similarity("text", "text", scorer, resume_owner="", jd_owner="job")
        with self.assertRaises(ValueError):
            semantic.section_similarity("text", " ", scorer, resume_owner="resume", jd_owner="job")

    def test_fuzzy_skill_selection_is_high_threshold_one_to_one_and_blocked(self):
        pairs = {
            ("Neural networks", "Machine learning"): 0.96,
            ("JavaScript", "Java"): 0.99,
            ("Scala", "Java"): 0.91,
        }
        matches = semantic.select_fuzzy_matches(
            ["Machine learning", "Java"], ["Neural networks", "JavaScript", "Scala"], pairs,
        )
        self.assertEqual(matches, [{"jd_skill": "Neural networks", "resume_skill": "Machine learning",
                                    "cosine": 0.96, "type": "fuzzy"}])

    def test_fuzzy_matching_rejects_invalid_pairs_and_thresholds(self):
        with self.assertRaises(ValueError):
            semantic.select_fuzzy_matches(["Machine learning"], ["Neural networks"], None)
        for pairs in ({("JavaScript", "Java"): 0.99},
                      {("Neural networks", "Machine learning"): 0.91}):
            with self.subTest(pairs=pairs):
                self.assertEqual(semantic.select_fuzzy_matches(
                    ["Machine learning", "Java"], ["Neural networks", "JavaScript"], pairs), [])

    def test_unknown_jd_skill_matches_resume_skill_section_phrase(self):
        scorer = FakeScorer({"AcmeCloud": vector(1), "Acme Cloud Platform": vector(1),
                             "Resume heading": vector(0, 1)})
        matches = semantic.fuzzy_unknown_skill_matches(
            "Skills\nAcme Cloud Platform, Python\nExperience\nResume heading",
            "Requirements\nAcmeCloud platform required", scorer,
            resume_owner="resume:hash", jd_owner="jd:hash")
        self.assertEqual(matches, [{"jd_skill": "AcmeCloud", "resume_skill": "Acme Cloud Platform",
                                    "cosine": 1.0, "type": "fuzzy"}])
        self.assertTrue(all("Acme Cloud Platform" not in owner for _, owner in scorer.calls))

    def test_alias_proposals_require_high_margin_and_are_not_applied(self):
        from backend.matching_extraction import SKILLS

        class Scorer:
            def embedding(self, text, owner):
                vector_value = vector(1) if text == "UnknownTech" or text.casefold() in {
                    alias.casefold() for alias in SKILLS["Kotlin"]} else vector(0, 1)
                return vector_value, []

        candidates = [{"skill": "UnknownTech", "count": 3, "first_seen": "2026-01-01", "last_seen": "2026-02-01"}]
        proposals = semantic.propose_skill_aliases(candidates, Scorer())
        self.assertEqual(proposals, [{"alias": "UnknownTech", "canonical_skill": "Kotlin",
                                     "cosine": 1.0, "count": 3}])
        self.assertNotIn("UnknownTech", SKILLS["Kotlin"])

    def test_alias_proposals_reject_known_terms_and_ambiguous_vectors(self):
        from backend.matching_extraction import SKILLS

        class AmbiguousScorer:
            def embedding(self, text, owner):
                return vector(1), []

        known = [{"skill": "Python", "count": 2, "first_seen": "a", "last_seen": "b"}]
        with self.assertRaises(ValueError):
            semantic.propose_skill_aliases(known, AmbiguousScorer())
        unknown = [{"skill": "UnknownTech", "count": 2, "first_seen": "a", "last_seen": "b"}]
        self.assertEqual(semantic.propose_skill_aliases(unknown, AmbiguousScorer()), [])
        self.assertNotIn("UnknownTech", SKILLS)


if __name__ == "__main__":
    unittest.main()
