"""Regression coverage for the supplied extraction patch."""

import subprocess
import sys
import unittest
from datetime import date

from backend import matching_extraction as extraction


class ExtractionPatchTests(unittest.TestCase):
    def test_unknown_token_completes_within_timeout(self):
        subprocess.run(
            [sys.executable, "-B", "-c",
             "from backend import matching_extraction as x; "
             "x.extract_jd_requirements('Requirements\\nExperience with software ' + 'AB'*15 + '_ x')"],
            timeout=5, check=True,
        )

    def test_tools_and_latency_are_not_degrees(self):
        for text in ("MS Excel", "MS SQL Server", "100 ms", "MS Access"):
            with self.subTest(text=text):
                self.assertNotIn("masters", extraction.extract_education(text))

    def test_common_degrees(self):
        for level, degrees in (
            ("bachelors", ("B.E. in Engineering", "BE Computer Science", "B.Sc", "B.Com", "BBA", "Bachelor of Science")),
            ("masters", ("M.Sc", "M.E. Computer Science", "Master of Science", "MS in Computing")),
        ):
            for degree in degrees:
                with self.subTest(degree=degree):
                    self.assertIn(level, extraction.extract_education(degree))

    def test_current_dates_update_duration_and_cache_dependency(self):
        for ending in ("Till Date", "Currently", "Ongoing", "Current", "To Date"):
            text = f"Experience\nDeveloper Jan 2020 - {ending}"
            with self.subTest(ending=ending):
                self.assertGreater(extraction.experience_years(text, date(2026, 11, 13)), 5)
                self.assertTrue(extraction.has_current_experience_date(text))

    def test_apostrophe_dates(self):
        text = "Experience\nDeveloper Jan'20 - Dec'24"
        self.assertEqual(extraction.experience_years(text, date(2026, 11, 13)), 5)
        self.assertFalse(extraction.has_current_experience_date(text))

    def test_heading_aliases(self):
        for section, headings in (
            ("experience", ("Work History", "Employment", "Career History", "Relevant Experience", "Internships")),
            ("skills", ("Key Skills", "Skills & Tools", "Tech Stack", "Technologies", "Core Competencies")),
            ("requirements", ("Key Requirements", "Required Skills", "Who you are")),
            ("nice_to_have", ("Good to have", "Nice-to-have", "Preferred")),
        ):
            for heading in headings:
                with self.subTest(heading=heading):
                    self.assertEqual(extraction.split_sections(heading + "\nPython")[0]["name"], section)
