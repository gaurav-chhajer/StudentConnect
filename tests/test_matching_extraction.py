import unittest
from datetime import date

from backend import matching_extraction as extraction


class MatchingExtractionTests(unittest.TestCase):
    def test_optional_scope_and_negated_required_heading(self):
        items = extraction.extract_jd_requirements("Requirements: Python required. Additional Experience (Not Required): C++. Docker.")
        self.assertEqual([(q["skills"], q["priority"]) for q in items],
                         [(["Python"], "must_have"), (["C++"], "nice_to_have"), (["Docker"], "nice_to_have")])
        self.assertEqual(extraction.extract_jd_requirements("Requirements\nC++ is optional")[0]["priority"], "nice_to_have")
        multiline = extraction.extract_jd_requirements("Requirements\nPython\nAdditional Experience (Not Required):\nC++\nDocker")
        self.assertEqual([q["priority"] for q in multiline], ["must_have", "nice_to_have", "nice_to_have"])

    def test_room_growth_is_not_android_skill(self):
        self.assertEqual(extraction.extract_skill_mentions("Requirements\nRoom to grow professionally"), [])
        self.assertEqual(extraction.extract_skill_mentions("Requirements\nRoom database")[0]["skill"], "Room")

    def test_abbreviation_does_not_split_degree_clause(self):
        items = extraction.extract_jd_requirements("Requirements: Bachelor's degree in a related field (e.g. Information Technology). Python required.")
        self.assertEqual(len(items), 2)
        self.assertIn("Information Technology", items[0]["text"])

    def test_years_and_optional_priority_stay_with_local_bullet(self):
        items = extraction.extract_jd_requirements("Requirements: - At least 3 years of Python - Docker - SQL preferred")
        self.assertEqual([(q["skills"], q["minimum_years"], q["priority"]) for q in items],
                         [(["Python"], 3, "must_have"), (["Docker"], None, "must_have"), (["SQL"], None, "nice_to_have")])

    def test_experience_clauses_do_not_share_preferred_cue(self):
        items = extraction.extract_jd_requirements("Requirements: Strong Java development experience Experience with SQL preferred")
        self.assertEqual([(q["skills"], q["priority"]) for q in items],
                         [(["Java"], "must_have"), (["SQL"], "nice_to_have")])

    def test_flattened_required_and_preferred_sentences_keep_priorities(self):
        items = extraction.extract_jd_requirements("Python is required. Docker is preferred.")
        self.assertEqual([(item["skills"], item["priority"], item["line"]) for item in items],
                         [(["Python"], "must_have", 1), (["Docker"], "nice_to_have", 1)])

    def test_inline_headings_and_technical_dots(self):
        items = extraction.extract_jd_requirements(
            "Requirements: Node.js and .NET; Python. Preferred qualifications: Docker.")
        self.assertEqual(items[0]["skills"], [".NET", "Node.js"])
        self.assertEqual(items[1]["skills"], ["Python"])
        self.assertEqual([item["priority"] for item in items], ["must_have", "must_have", "nice_to_have"])

    def test_flattened_and_multiline_requirements_match(self):
        lines = "Requirements:\nPython required.\nDocker required.\nPreferred qualifications:\nAWS preferred."
        compact = lambda text: [(q["skills"], q["priority"]) for q in extraction.extract_jd_requirements(text)]
        self.assertEqual(compact(lines), compact(lines.replace("\n", " ")))

    def test_clauses_preserve_decimal_years_and_source_lines(self):
        items = extraction.extract_jd_requirements("Overview\nRequirements: 3.5 years of Python required. Docker preferred.")
        self.assertEqual(items[0]["minimum_years"], 3.5)
        self.assertEqual([item["line"] for item in items], [2, 2])

    def test_degree_clause_is_not_applied_to_adjacent_skills(self):
        from backend import deterministic_scoring
        result = deterministic_scoring.score("Skills\nPython", "Requirements: Python. Master's degree required.",
                                             0.5, as_of_date=date(2026, 11, 13))
        self.assertEqual(result["must_have_criterion_count"], 2)
        self.assertEqual(result["missing_must_have_criterion_count"], 1)

    def test_unknown_skill_candidates_only_come_from_prioritized_jd_lines(self):
        text = ("Summary\nAcmeCloud is our office system\nRequirements\n"
                "Experience with the AcmeCloud platform and QuantumDB database required")
        self.assertEqual(extraction.unknown_skill_candidates(text), ["AcmeCloud", "QuantumDB"])

    def test_unknown_company_names_and_business_acronyms_are_not_scoreable_skills(self):
        text = ("Requirements\nExperience with MeeCare and www.meesho required\n"
                "Must have BNPL, BRE, DPD, EAD, ECL, GNPA, LGD, NPA, RCA, and KRs")
        self.assertEqual(extraction.unknown_skill_candidates(text), [])

    def test_generic_application_language_does_not_make_domain_acronyms_technical_skills(self):
        text = "Requirements\nApply knowledge of PD, EAD, and LGD models to evaluate credit risk required"
        requirement, = extraction.extract_jd_requirements(text)
        self.assertEqual(requirement["unknown_skills"], [])

    def test_sections_keep_line_numbers_and_recognize_common_headers(self):
        sections = extraction.split_sections(
            "Candidate summary\nPython developer\n\nTECHNICAL SKILLS:\nPython, SQL\n"
            "Professional Experience\nBuilt Python services\nEducation\nB.Tech Computer Science"
        )
        self.assertEqual([part["name"] for part in sections],
                         ["other", "skills", "experience", "education"])
        self.assertEqual(sections[1]["start_line"], 5)
        self.assertEqual(sections[2]["start_line"], 7)
        self.assertIn("Built Python services", sections[2]["text"])

    def test_skill_boundaries_and_section_aware_evidence(self):
        mentions = extraction.extract_skill_mentions(
            "Skills\nJava, JavaScript, C++, C#, React.js\nProjects\nUsed React.js and Python"
        )
        by_skill = {item["skill"]: item for item in mentions}
        self.assertEqual(set(by_skill), {"Java", "JavaScript", "C++", "C#", "React", "Python"})
        self.assertEqual(by_skill["Java"]["matched_text"], "Java")
        self.assertEqual(by_skill["React"]["section"], "projects")
        self.assertEqual(by_skill["React"]["match_type"], "alias")
        self.assertEqual(by_skill["React"]["evidence_credit"], 0.75)
        self.assertEqual(len(extraction.extract_skill_mentions("Python\nPython", all_mentions=True)), 2)

    def test_supplemental_taxonomy_aliases_and_language_separation(self):
        mentions = extraction.extract_skill_mentions(
            "Scikit learn sklearn HuggingFace Transformers CI/CD LLMs GenAI Java JavaScript"
        )
        self.assertEqual({item["skill"] for item in mentions}, {
            "Scikit-learn", "Hugging Face Transformers", "CI/CD", "Large language models",
            "Generative AI", "Java", "JavaScript",
        })

    def test_versioned_android_taxonomy_and_framework_implications(self):
        self.assertEqual(extraction.TAXONOMY_VERSION, "skills-v3-esco-1.2.0")
        mentions = extraction.expand_implied_skills(
            extraction.extract_skill_mentions("Skills\nJetpack Compose, Coroutines, Hilt, Retrofit, Room, "
                                              "Firebase, Kafka, GraphQL, Terraform"))
        names = {item["skill"] for item in mentions}
        self.assertTrue({"Android", "Kotlin", "Kotlin Coroutines", "Hilt", "Retrofit", "Room",
                         "Firebase", "Kafka", "GraphQL", "Terraform"}.issubset(names))
        self.assertEqual({item["skill"] for item in extraction.expand_implied_skills(
            extraction.extract_skill_mentions("Requirements\nFlask and Pandas required"))},
            {"Flask", "Pandas", "Python"})

    def test_esco_aliases_are_limited_to_reviewed_exact_english_alternates(self):
        mentions = extraction.extract_skill_mentions(
            "C Sharp Cascading Style Sheets SEQUEL client-side JavaScript "
            "server-side JavaScript Mocha")
        self.assertEqual({item["skill"] for item in mentions}, {"C#", "CSS", "SQL", "JavaScript"})

    def test_alias_candidate_queue_ignores_common_resume_noise(self):
        text = "Requirements\nStrong proficiency with Bachelor skills required"
        self.assertEqual(extraction.unknown_skill_candidates(text), [])

    def test_directional_implications_are_partial_and_do_not_join_java_pairs(self):
        explicit = extraction.extract_skill_mentions("Neural networks JavaScript")
        implied = extraction.expand_implied_skills(explicit)
        by_skill = {item["skill"]: item for item in implied}
        self.assertEqual(by_skill["Machine learning"]["match_type"], "implies")
        self.assertEqual(by_skill["Machine learning"]["evidence_credit"], 0.4)
        self.assertNotIn("Java", by_skill)
        self.assertIn(frozenset(("Java", "JavaScript")), extraction.BLOCKED_SKILL_PAIRS)

    def test_education_evidence_keeps_level_field_and_line(self):
        evidence = extraction.extract_education_evidence(
            "Education\nB.Tech in Computer Science\nBachelors\n"
        )
        self.assertEqual(evidence[0]["line"], 2)
        self.assertEqual(evidence[0]["levels"], ["bachelors"])
        self.assertEqual(set(evidence[0]["fields"]), {"computer_science", "engineering"})

    def test_education_does_not_infer_degrees_from_it_or_office_phrases(self):
        self.assertEqual(extraction.extract_education_evidence("We love it when you use MS Office."), [])
        self.assertEqual(extraction.extract_education("Post-graduate"), ["masters"])

    def test_experience_duration_in_general_copy_is_not_a_jd_requirement(self):
        requirement, = extraction.extract_jd_requirements(
            "We have 15 years of experience serving customers.")
        self.assertEqual(requirement["priority"], "unclassified")

    def test_unknown_skill_candidates_do_not_treat_ordinary_titlecase_words_as_skills(self):
        requirement, = extraction.extract_jd_requirements(
            "Requirements\nWe have 15 years of experience serving customers and leading teams.")
        self.assertEqual(requirement["unknown_skills"], [])

    def test_sept_month_is_parsed_as_september(self):
        resume = "Experience\nDeveloper | Jun 2025 - Sept 2025"
        self.assertEqual(extraction.experience_years(resume, date(2026, 11, 11)), 0.33)

    def test_ambiguous_skill_words_need_technical_context(self):
        text = ("We excel in a fast-paced team; the rest of the day, react quickly.\n"
                "Spring 2024, swift delivery, sales, and marketing.")
        self.assertEqual(extraction.extract_skill_mentions(text), [])

    def test_requirement_priority_is_explicit_and_line_level(self):
        requirements = extraction.extract_jd_requirements(
            "Requirements\nPython required, at least 3 years experience\n"
            "Preferred Skills\nReact is a plus\nResponsibilities\nBuild software"
        )
        self.assertEqual(len(requirements), 2)
        self.assertEqual(requirements[0]["priority"], "must_have")
        self.assertEqual(requirements[0]["skills"], ["Python"])
        self.assertEqual(requirements[0]["minimum_years"], 3)
        self.assertEqual(requirements[1]["priority"], "nice_to_have")
        self.assertEqual(requirements[1]["skills"], ["React"])

    def test_year_requirements_are_must_have_without_a_heading(self):
        requirement, = extraction.extract_jd_requirements("At least 3 years of Python experience")
        self.assertEqual(requirement["priority"], "must_have")
        self.assertEqual(requirement["minimum_years"], 3)

    def test_unclassified_skills_and_title_are_fallback_requirements_only_without_cues(self):
        requirements = extraction.extract_jd_requirements(
            "We are hiring a Python developer.\nSQL and Docker are part of the stack.",
            job_title="Kotlin Engineer",
        )
        self.assertEqual({skill for item in requirements if item["priority"] == "inferred_must_have"
                          for skill in item["skills"]}, {"Python", "SQL", "Docker", "Kotlin"})

    def test_explicit_requirement_cues_disable_unclassified_fallback(self):
        requirements = extraction.extract_jd_requirements(
            "Python is required.\nWe also use SQL and Docker in the stack."
        )
        self.assertEqual({item["priority"] for item in requirements}, {"must_have", "unclassified"})

    def test_explicit_education_seniority_and_years_only(self):
        text = "Senior developer with 2 years experience and B.Tech in Computer Science"
        self.assertEqual(extraction.extract_education(text), ["bachelors"])
        self.assertEqual(extraction.extract_seniority(text), ["senior"])
        self.assertEqual(extraction.explicit_years_experience(text), [2])
        self.assertEqual(extraction.explicit_years_experience("Worked from 2020 to 2024"), [])
        self.assertEqual(extraction.explicit_years_experience("3.5-8 years of relevant work"), [3.5])

    def test_experience_date_ranges_merge_overlap_and_track_skills(self):
        resume = (
            "Experience\nPython engineer | Jan 2020 - Dec 2021\n"
            "Python and SQL developer | Jan 2021 - Dec 2022\n"
            "Projects\nPython developer | Jan 2015 - Dec 2019"
        )
        as_of = date(2024, 6, 1)
        self.assertEqual(extraction.experience_years(resume, as_of), 3.0)
        self.assertEqual(extraction.skill_experience_years(resume, as_of), {"Python": 3.0, "SQL": 2.0})

    def test_current_experience_date_uses_explicit_scoring_date(self):
        resume = "Work Experience\nPython Intern | Jun 2022 - Present"
        self.assertEqual(extraction.experience_years(resume, date(2024, 5, 1)), 2.0)
        with self.assertRaises(TypeError):
            extraction.experience_years(resume, "2024-05-01")

    def test_invalid_or_oversized_documents_fail_explicitly(self):
        with self.assertRaises(TypeError):
            extraction.normalize_text(None)
        with self.assertRaises(ValueError):
            extraction.split_sections("x" * 250001)
        with self.assertRaises(TypeError):
            extraction.extract_seniority(None)


if __name__ == "__main__":
    unittest.main()
