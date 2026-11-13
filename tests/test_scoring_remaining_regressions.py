"""Remaining scoring defects, using the repository's unittest runner."""
import unittest
from datetime import date

from backend import deterministic_scoring as scoring


class RemainingScoringTests(unittest.TestCase):
    def score(self, resume, jd, title="", semantic=0.6):
        return scoring.score(resume, jd, semantic, job_title=title, as_of_date=date(2026, 11, 13))

    def test_non_experience_years(self):
        for line in ("Must be at least 18 years of age", "Company has 20 years in business", "Plans for next 5 years"):
            with self.subTest(line=line):
                self.assertEqual(self.score("Skills\nPython", "Requirements\nPython\n" + line)["experience_evidence"], [])

    def test_title_precedes_incidental_seniority(self):
        resume = "Experience\nPython Developer Intern Jun 2025 - Dec 2025\nPython"
        jd = "Requirements\nPython"
        for title in ("Junior Python Developer", ""):
            self.assertEqual(self.score(resume, jd, title)["score"], self.score(resume, jd + "\nYou will work with senior engineers and report to the Engineering Manager.", title)["score"])

    def test_alternatives_are_one_criterion_and_keep_conjunctions(self):
        for skill in ("Python", "Java", "Go"):
            result = self.score("Skills\n" + skill, "Requirements\nPython or Java or Go\nDocker")
            self.assertEqual(result["missing_must_have"], ["Docker"])
            self.assertEqual(result["must_have_criterion_count"], 2)
        missing = self.score("Skills\nPython", "Requirements\nJava or Go")["missing_must_have"]
        self.assertEqual(len(missing), 1)

    def test_implied_experience(self):
        result = self.score("Experience\nDeveloper Jan 2018 - Present\nBuilt Django services", "Requirements\n3+ years of Python")
        self.assertGreaterEqual(result["experience_evidence"][0]["observed_years"], 3)

    def test_unknown_acronyms(self):
        result = self.score("Experience\nBuilt Python REST web services", "Requirements\nPython\nFamiliarity with JSON and HTTP in web services")
        self.assertFalse(result["hard_cap_applied"])
        listed = scoring.extraction.extract_jd_requirements("Requirements\nSkills: JSON, HTTP")
        self.assertEqual(listed[0]["unknown_skills"], ["HTTP", "JSON"])

    def test_outcome_evidence_reaches_ninety(self):
        resume = "Skills\nPython, Django, Docker\nExperience\nSenior Engineer Jan 2018 - Present\nBuilt Python Django services deployed with Docker, cutting latency 40%\nEducation\nB.Tech Computer Science"
        jd = "Requirements\n3+ years of Python\nDjango\nBachelor's degree in Computer Science\nPreferred qualifications\nDocker"
        self.assertGreaterEqual(self.score(resume, jd, "Senior Backend Engineer", 0.8)["score"], 90)

    def test_keyword_list_has_limited_credit(self):
        jd = "Requirements\n3+ years of Python\nDjango\nKubernetes\nTerraform\nPreferred qualifications\nDocker"
        honest = "Skills\nPython\nExperience\nIntern Jun 2025 - Aug 2025\nWrote Python scripts\nEducation\nB.Tech Computer Science"
        stuffed = honest + "\nProjects\nPython Django Kubernetes Terraform Docker (all of the above)"
        self.assertLessEqual(self.score(stuffed, jd)["score"] - self.score(honest, jd)["score"], 10)

    def test_long_summary_is_bounded_and_tampering_rejected(self):
        result = self.score("Skills\nPython", "Requirements\nRequired: " + "operating Kubernetes and Terraform at scale, " * 8)
        missing = result["missing_must_have"] + [item["jd"] for item in result["missing_must_have_requirements"]]
        adapted = dict(fit_score=result["score"], reasoning=scoring.template_explanation(result), matched_skills=[], missing_requirements=missing, scorer_version=result["scorer_version"], weights_version=result["weights_version"], deterministic_breakdown=result)
        validated = scoring.validate_adapted_result(adapted)
        self.assertTrue(all(len(item) <= 200 for item in validated["missing_requirements"]))
        adapted["missing_requirements"] = ["invented"]
        with self.assertRaises(ValueError):
            scoring.validate_adapted_result(adapted)
