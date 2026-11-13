import unittest
from datetime import date
from backend import deterministic_scoring as scoring, matching_extraction as extraction


class EvidenceSafetyTests(unittest.TestCase):
    def test_machine_learning_is_not_a_learning_aspiration(self):
        result = self.score("Experience\nBuilt Machine learning services", "Requirements\nMachine learning")
        self.assertEqual(result["components"]["must_have"], 1.0)
        novice = self.score("Experience\nCurrently learning Machine learning", "Requirements\nMachine learning")
        self.assertLess(novice["score"], result["score"])

    def test_new_weaker_evidence_does_not_dilute_existing_work(self):
        jd = "Requirements\nPython\nMachine learning"
        resume = "Experience\nBuilt Python services, reducing latency 40%"
        baseline = self.score(resume, jd)
        improved = self.score(resume + "\nBuilt Machine learning services", jd)
        self.assertGreaterEqual(improved["score"], baseline["score"])

    def test_removing_a_keyword_does_not_upgrade_remaining_keywords(self):
        jd = "Requirements\nPython\nDocker"
        original = self.score("Experience\nPython Docker", jd)
        removed = self.score("Experience\nPython", jd)
        self.assertLessEqual(removed["score"], original["score"])

    def test_weak_explicit_skill_does_not_block_stronger_implied_evidence(self):
        result = self.score("Experience\nBuilt Pandas services\nPython (beginner)", "Requirements\nPython")
        removed = self.score("Experience\nBuilt Pandas services", "Requirements\nPython")
        self.assertEqual(result["score"], removed["score"])

    def test_machine_learning_in_skill_list_does_not_discount_other_skills(self):
        mentions = {q["skill"]: q for q in extraction.extract_skill_mentions("Skills\nMachine learning, Python")}
        self.assertEqual(mentions["Python"]["evidence_credit"], 0.4)

    def score(self, resume, jd="Requirements\nDocker", title=""):
        return scoring.score(resume, jd, 0.6, job_title=title, as_of_date=date(2026, 11, 13))

    def test_negated_and_learning_evidence_is_not_production(self):
        good = self.score("Experience\nBuilt Docker services")
        for phrase in ("No experience with Docker", "Have never used Docker"):
            with self.subTest(phrase=phrase):
                self.assertIn("Docker", self.score("Experience\n" + phrase)["missing_must_have"])
        for phrase in ("Currently learning Docker", "Docker (beginner)", "familiar with Docker"):
            self.assertLess(self.score("Experience\n" + phrase)["score"], good["score"])

    def test_unknown_labels_are_visible_and_require_review(self):
        absent = self.score("Skills\nPython", "Requirements\nSalesforce\nApex")
        present = self.score("Skills\nSalesforce, Apex", "Requirements\nSalesforce\nApex")
        self.assertEqual(set(absent["missing_must_have"]), {"Salesforce", "Apex"})
        self.assertLess(absent["score"], present["score"])
        self.assertTrue(absent["requirement_coverage"]["needs_review"])

    def test_normalized_aliases_and_units(self):
        got = {q["skill"] for q in extraction.extract_skill_mentions("Skills\nPython3 JS TS VueJS AngularJS html5 CICD Mongo Node JS Machine\u00a0\u00a0learning GO")}
        self.assertTrue({"Python", "JavaScript", "TypeScript", "Vue", "Angular", "HTML", "CI/CD", "MongoDB", "Node.js", "Machine learning", "Go"} <= got)

    def test_seniority_deficit_is_a_hard_gap(self):
        result = self.score("Experience\nIntern\nBuilt Docker services", title="Lead Engineer")
        self.assertLessEqual(result["score"], 60)
        self.assertIn("seniority", {q["reason"] for q in result["missing_must_have_requirements"]})

    def test_clause_stress_completes(self):
        import subprocess
        import sys
        subprocess.run([sys.executable, "-B", "-c", "from backend.matching_extraction import extract_jd_requirements; extract_jd_requirements(('(e.g. Foo. ' * 20000)[:240000])"], check=True, timeout=10)
