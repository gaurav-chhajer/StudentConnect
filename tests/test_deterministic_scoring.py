import unittest
from datetime import date

from backend import deterministic_scoring as scoring


class DeterministicScoringTests(unittest.TestCase):
    def test_weights_and_result_are_deterministic_and_structured(self):
        resume = "Skills\nPython\nProjects\nBachelors degree; Python APIs"
        jd = "Requirements\nPython required\nBachelor's degree required"
        result = scoring.score(resume, jd, 0.8, job_title="Software Engineer", as_of_date=date(2026, 11, 11))
        self.assertEqual(sum(scoring.WEIGHTS.values()), 100)
        self.assertEqual(result, scoring.score(resume, jd, 0.8, job_title="Software Engineer",
                                               as_of_date=date(2026, 11, 11)))
        self.assertEqual(result["score"], 84)
        self.assertEqual(result["components"]["must_have"], 1.0)
        self.assertEqual(result["matched"][0]["type"], "exact")
        self.assertEqual(result["scorer_version"], "deterministic-v4")

    def test_cache_version_changes_daily_only_for_current_experience(self):
        static_resume = "Experience\nPython developer | Jan 2020 - Dec 2022"
        current_resume = "Experience\nPython developer | Jan 2020 - Present"
        first = date(2026, 11, 10)
        second = date(2026, 11, 11)
        self.assertEqual(scoring.cache_version(static_resume, first, "model"),
                         scoring.cache_version(static_resume, second, "model"))
        self.assertNotEqual(scoring.cache_version(current_resume, first, "model"),
                            scoring.cache_version(current_resume, second, "model"))

    def test_presence_satisfies_coverage_and_demonstration_improves_evidence(self):
        listed = scoring.score("Skills\nPython", "Requirements\nPython required", 0.5,
                               as_of_date=date(2026, 11, 11))
        demonstrated = scoring.score("Projects\nPython project", "Requirements\nPython required", 0.5,
                                     as_of_date=date(2026, 11, 11))
        self.assertEqual(listed["components"]["must_have"], 0.4)
        self.assertEqual(demonstrated["components"]["must_have"], 1.0)
        self.assertGreater(demonstrated["score"], listed["score"])
        self.assertGreater(demonstrated["components"]["experience"], listed["components"]["experience"])

    def test_verified_spelling_variants_satisfy_required_skills(self):
        result = scoring.score("Skills\nPowerBI, SpringBoot, Data Analytics",
                               "Requirements\nPower BI\nSpring\nData analysis", 0.5,
                               as_of_date=date(2026, 11, 13))
        self.assertEqual(result["missing_must_have"], [])
        self.assertEqual(result["components"]["must_have"], 0.4)

    def test_related_tools_are_not_unconditional_skill_aliases(self):
        mentions = scoring.extraction.extract_skill_mentions("Skills\nGitHub, Jenkins pipelines")
        self.assertNotIn("Git", {item["skill"] for item in mentions})
        self.assertNotIn("CI/CD", {item["skill"] for item in mentions})

    def test_numeric_month_dates_preserve_duration(self):
        numeric = "Experience\nPython Developer 06/2022 - 05/2024"
        named = "Experience\nPython Developer Jun 2022 - May 2024"
        as_of = date(2026, 11, 13)
        self.assertEqual(scoring.extraction.experience_years(numeric, as_of), 2.0)
        self.assertEqual(scoring.extraction.skill_experience_years(numeric, as_of),
                         scoring.extraction.skill_experience_years(named, as_of))

    def test_contextual_skill_evidence_matches_and_keeps_source(self):
        for text, skill, label in (
            ("Skills\nML", "Machine learning", "ML"),
            ("Experience\nTrained ML models for classification", "Machine learning", "ML"),
            ("Experience\nManaged GitHub branches and pull requests", "Git", "GitHub"),
            ("Experience\nAutomated Jenkins pipelines for deployment", "CI/CD", "Jenkins pipelines"),
        ):
            with self.subTest(text=text):
                result = scoring.score(text, "Requirements\n" + skill, 0.5, as_of_date=date(2026, 11, 13))
                self.assertEqual(result["missing_must_have"], [])
                match = next(item for item in result["matched"] if item.get("skill") == skill)
                self.assertEqual(match["resume"], label)
                self.assertGreater(match["resume_line"], 0)

    def test_contextual_labels_do_not_invent_skills(self):
        for text in ("Skills\nGitHub, Jenkins pipelines", "Contact\nhttps://github.com/branches",
                     "Experience\nUsed 500 ML of solution", "Summary\nRead ML news"):
            with self.subTest(text=text):
                names = {item["skill"] for item in scoring.extraction.extract_skill_mentions(text)}
                self.assertFalse(names & {"Git", "CI/CD", "Machine learning"})

    def test_semantic_component_and_must_have_cap(self):
        resume = "Projects\nPython project"
        jd = "Requirements\nPython required\nSQL required\nKubernetes required"
        result = scoring.score(resume, jd, 0.9, as_of_date=date(2026, 11, 11))
        self.assertEqual(result["missing_must_have"], ["Kubernetes", "SQL"])
        self.assertTrue(result["hard_cap_applied"])
        self.assertLessEqual(result["score"], 60)
        self.assertEqual(result["components"]["semantic"], 0.9)

    def test_missing_required_experience_counts_toward_the_hard_cap(self):
        resume = "Experience\nPython developer | Jan 2024 - Dec 2024"
        jd = "At least 5 years experience"
        result = scoring.score(resume, jd, 0.9, as_of_date=date(2026, 11, 11))
        self.assertTrue(result["hard_cap_applied"])
        self.assertEqual(result["missing_must_have_criterion_count"], 1)
        self.assertEqual(result["missing_must_have_requirements"][0]["reason"], "experience_years")
        self.assertGreater(result["score"], 40)
        self.assertLessEqual(result["score"], 60)
        self.assertTrue(result["experience_hard_cap_applied"])

    def test_zero_year_requirement_does_not_divide_by_zero_or_reduce_experience_fit(self):
        result = scoring.score("General resume", "Requirements\n0 years experience", 0.5,
                               as_of_date=date(2026, 11, 11))
        self.assertEqual(result["components"]["experience"], 1.0)
        self.assertEqual(result["experience_evidence"][0]["required_years"], 0)
        self.assertFalse(result["hard_cap_applied"])

    def test_plain_language_job_description_separates_strong_and_weak_resumes(self):
        jd = "We are hiring a Python developer. SQL and Docker are part of the stack."
        strong = scoring.score(
            "Experience\nPython developer using SQL and Docker in production.", jd, 0.5,
            job_title="Python Developer", as_of_date=date(2026, 11, 11))
        weak = scoring.score(
            "Experience\nCustomer support representative serving clients.", jd, 0.5,
            job_title="Python Developer", as_of_date=date(2026, 11, 11))
        self.assertGreater(strong["score"], weak["score"])
        self.assertEqual(strong["missing_inferred_skills"], [])

    def test_unclassified_job_title_skill_is_a_lighter_inferred_requirement(self):
        result = scoring.score("Experience\nCustomer support representative.",
                               "Join our engineering team.", 0.5, job_title="Kotlin Engineer",
                               as_of_date=date(2026, 11, 11))
        self.assertEqual(result["inferred_must_have_skills"], ["Kotlin"])
        self.assertEqual(result["missing_inferred_skills"], ["Kotlin"])

    def test_skill_experience_inherits_role_header_dates(self):
        resume = ("Experience\nAndroid Developer | Jan 2021 - Dec 2023\n"
                  "- Built Kotlin applications for Android devices.")
        self.assertEqual(scoring.extraction.skill_experience_years(
            resume, date(2026, 11, 11))["Kotlin"], 3.0)

    def test_unobserved_skill_groups_receive_no_positive_default_credit(self):
        result = scoring.score("General student resume", "General internship description", 0.5,
                               as_of_date=date(2026, 11, 11))
        self.assertEqual(result["components"]["must_have"], 0.4)
        self.assertEqual(result["components"]["nice_to_have"], 0.4)

    def test_years_education_seniority_and_missing_requirement_semantics(self):
        resume = "Experience\nPython developer | Jan 2022 - Dec 2023\nEducation\nB.Tech"
        jd = "Requirements\nAt least 4 years experience\nMaster's degree required"
        result = scoring.score(resume, jd, 0.6, job_title="Senior Data Engineer", as_of_date=date(2026, 11, 11))
        self.assertEqual(result["components"]["experience"], 0.5)
        self.assertEqual(result["components"]["education_title"], 0.35)

    def test_implied_skill_and_education_matches_include_source_lines(self):
        resume = "Skills\nNeural networks\nEducation\nB.Tech in Computer Science"
        jd = "Requirements\nMachine learning required\nBachelors degree in Computer Science required"
        result = scoring.score(resume, jd, 0.6, as_of_date=date(2026, 11, 11))
        self.assertEqual({item["type"] for item in result["matched"]}, {"implies", "education"})
        self.assertTrue(all(item["jd_line"] > 0 and item["resume_line"] > 0 for item in result["matched"]))

    def test_fuzzy_skill_matches_require_real_text_and_high_cosine(self):
        result = scoring.score(
            "Skills\nJava", "Requirements\nPython required", 0.6,
            as_of_date=date(2026, 11, 11),
            fuzzy_skill_pairs=[{"jd_skill": "Python", "resume_skill": "Java", "cosine": 0.95,
                                "type": "fuzzy"}],
        )
        self.assertEqual(result["matched"][0]["type"], "fuzzy")
        with self.assertRaises(ValueError):
            scoring.score("Skills\nJava", "Requirements\nJavaScript required", 0.6,
                          as_of_date=date(2026, 11, 11),
                          fuzzy_skill_pairs=[{"jd_skill": "JavaScript", "resume_skill": "Java",
                                              "cosine": 0.99, "type": "fuzzy"}])

    def test_unknown_jd_skill_can_match_a_raw_resume_skill_phrase(self):
        result = scoring.score(
            "Skills\nAcme Cloud Platform", "Requirements\nAcmeCloud platform required", 0.6,
            as_of_date=date(2026, 11, 11),
            fuzzy_skill_pairs=[{"jd_skill": "AcmeCloud", "resume_skill": "Acme Cloud Platform",
                                "cosine": 0.95, "type": "fuzzy"}],
        )
        self.assertEqual(result["missing_must_have"], [])
        self.assertEqual(result["matched"][0]["type"], "fuzzy")
        self.assertEqual(result["matched"][0]["resume"], "Acme Cloud Platform")

    def test_unknown_must_have_without_resume_evidence_is_reported(self):
        result = scoring.score("Skills\nPython", "Requirements\nAcmeCloud platform required", 0.8,
                               as_of_date=date(2026, 11, 11))
        self.assertEqual(result["missing_must_have"], ["AcmeCloud"])

    def test_invalid_inputs_and_cosine_fail_closed(self):
        base = ("Python resume", "Python required", 0.5)
        for cosine in (True, None, "0.5", float("nan"), float("inf"), -1.1, 1.1):
            with self.subTest(cosine=cosine), self.assertRaises(ValueError):
                scoring.score(base[0], base[1], cosine, as_of_date=date(2026, 1, 1))
        with self.assertRaises(ValueError):
            scoring.score(" ", base[1], base[2], as_of_date=date(2026, 1, 1))
        with self.assertRaises(TypeError):
            scoring.score(base[0], base[1], base[2], as_of_date="2026-01-01")


if __name__ == "__main__":
    unittest.main()
