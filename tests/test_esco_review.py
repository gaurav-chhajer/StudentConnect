import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from unittest.mock import patch

from backend import deterministic_store
from backend.esco_review import exact_candidates, load_catalog, read_catalog
from scripts import run_weekly_alias_review
from scripts.run_weekly_alias_review import build_report


RDF = b'''<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
 xmlns:skos="http://www.w3.org/2004/02/skos/core#"
 xmlns:thes="http://purl.org/iso25964/skos-thes#">
 <skos:Concept rdf:about="http://data.europa.eu/esco/skill/python">
  <skos:prefLabel xml:lang="en">Python</skos:prefLabel><skos:altLabel xml:lang="en">Python programming</skos:altLabel>
 </skos:Concept>
 <skos:Concept rdf:about="http://data.europa.eu/esco/skill/old">
  <skos:prefLabel xml:lang="en">Old skill</skos:prefLabel><thes:status>obsolete</thes:status>
 </skos:Concept>
 <skos:Concept rdf:about="http://data.europa.eu/esco/occupation/dev">
  <skos:prefLabel xml:lang="en">Developer</skos:prefLabel>
 </skos:Concept>
</rdf:RDF>'''


class EscoReviewTests(unittest.TestCase):
    def test_checked_in_catalog_is_complete_and_review_only(self):
        catalog = load_catalog()
        self.assertEqual(len(catalog), 14359)
        self.assertTrue(any(item["preferred_label"] == "Python (computer programming)" for item in catalog))

    def test_stream_parser_keeps_active_english_skills_only(self):
        self.assertEqual(read_catalog(io.BytesIO(RDF)), [{"uri": "http://data.europa.eu/esco/skill/python",
                         "preferred_label": "Python", "alternate_labels": ["Python programming"]}])

    def test_exact_candidates_are_review_only_and_keep_uri(self):
        catalog = read_catalog(io.BytesIO(RDF))
        items = [{"skill": "Python programming", "count": 4, "first_seen": "a", "last_seen": "b"}]
        candidates = exact_candidates(items, catalog)
        self.assertEqual(candidates[0]["esco_candidates"][0]["uri"], "http://data.europa.eu/esco/skill/python")

    def test_report_includes_unmatched_terms_without_changing_taxonomy(self):
        conn = type("Conn", (), {})()
        with patch("scripts.run_weekly_alias_review.deterministic_store.pending_alias_candidates", return_value=[
                {"skill": "Python programming", "count": 4, "first_seen": "a", "last_seen": "b"},
                {"skill": "niche tool", "count": 2, "first_seen": "a", "last_seen": "b"}]) as pending:
            report = build_report(conn, read_catalog(io.BytesIO(RDF)), generated_on=__import__("datetime").date(2026, 1, 1))
        self.assertTrue(report["review_only"])
        self.assertFalse(report["active_taxonomy_modified"])
        self.assertEqual([item["unknown_skill"] for item in report["candidates"]], ["Python programming"])
        self.assertEqual([item["skill"] for item in report["unmatched_without_exact_esco_label"]], ["niche tool"])
        pending.assert_called_once()

    def test_weekly_command_writes_a_dated_report_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            database = Path(directory) / "cache.sqlite"
            output_dir = Path(directory) / "reports"
            with closing(sqlite3.connect(database)) as conn:
                deterministic_store.initialize(conn)
                deterministic_store.note_unmatched(conn, ["Python (computer programming)"])
                deterministic_store.note_unmatched(conn, ["Python (computer programming)"])
            report_path = output_dir / "alias-review-2026-01-04.json"
            with patch.object(run_weekly_alias_review, "date") as clock:
                clock.today.return_value = date(2026, 1, 4)
                self.assertEqual(run_weekly_alias_review.main([
                    "--database", str(database), "--output-dir", str(output_dir)]), 0)
                with report_path.open(encoding="utf-8") as saved:
                    report = json.load(saved)
                self.assertEqual(len(report["candidates"]), 1)
                with self.assertRaises(FileExistsError):
                    run_weekly_alias_review.main([
                        "--database", str(database), "--output-dir", str(output_dir)])


if __name__ == "__main__":
    unittest.main()
