import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from backend.migrate import apply_migrations
from backend.pipeline_skeleton import SCHEMA
from scripts.backfill_deterministic_scores import backfill


def result(score):
    names = ("must_have", "nice_to_have", "semantic", "experience", "education_title")
    return {"fit_score": score, "reasoning": "Local deterministic fit.", "matched_skills": ["Python"],
            "missing_requirements": [], "deterministic_breakdown": {
                "score": score, "raw_score": score, "components": dict.fromkeys(names, 0.7),
                "component_points": dict.fromkeys(names, 7), "weights": dict.fromkeys(names, 10),
                "matched": [{"jd": "Python", "resume": "Python", "type": "exact"}],
                "missing_must_have_requirements": [], "hard_cap_applied": False,
                "experience_hard_cap_applied": False}}


class DeterministicBackfillTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.conn = sqlite3.connect(self.root / "jobs.db")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript(SCHEMA)
        apply_migrations(self.conn)
        self.conn.execute("INSERT INTO users(id,name,email,resume_text) VALUES(1,'Test','test@example.test','Python')")
        self.conn.execute("INSERT INTO companies(id,name,ats_type,board_slug) VALUES(1,'Example','lever','example')")
        self.conn.execute("""INSERT INTO jobs(id,company_id,external_id,title,raw_json,first_seen_at,last_seen_at)
            VALUES(1,1,'job-1','Engineer','{}','2026-01-01','2026-01-01')""")
        self.conn.execute("""INSERT INTO scores(job_id,user_id,fit_score,reasoning,scored_at,is_stale)
            VALUES(1,1,70,'Prior score','2026-01-02',1)""")
        self.conn.commit()
        self.scorer = object()

    def test_preview_passes_gate_without_mutating_scores(self):
        with patch("scripts.backfill_deterministic_scores.pipeline.score_job_deterministic",
                   return_value=result(75)):
            summary = backfill(self.conn, self.scorer, backup_dir=self.root / "backups")
        self.assertEqual(summary["rows"], 1)
        self.assertFalse(summary["applied"])
        row = self.conn.execute("SELECT fit_score,is_stale,deterministic_json FROM scores").fetchone()
        self.assertEqual(tuple(row), (70, 1, None))
        self.assertFalse((self.root / "backups").exists())

    def test_lower_score_aborts_entire_apply_before_backup_or_write(self):
        with patch("scripts.backfill_deterministic_scores.pipeline.score_job_deterministic",
                   return_value=result(69)):
            with self.assertRaisesRegex(ValueError, "non-regression"):
                backfill(self.conn, self.scorer, apply=True, backup_dir=self.root / "backups")
        row = self.conn.execute("SELECT fit_score,is_stale,deterministic_json FROM scores").fetchone()
        self.assertEqual(tuple(row), (70, 1, None))
        self.assertFalse((self.root / "backups").exists())

    def test_apply_backs_up_then_saves_breakdown_and_current_score_atomically(self):
        with patch("scripts.backfill_deterministic_scores.pipeline.score_job_deterministic",
                   return_value=result(75)):
            summary = backfill(self.conn, self.scorer, apply=True, backup_dir=self.root / "backups")
        self.assertTrue(summary["applied"])
        backup = Path(summary["backup"])
        self.assertTrue(backup.is_file())
        with closing(sqlite3.connect(backup)) as saved:
            self.assertEqual(saved.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(saved.execute("SELECT fit_score,is_stale FROM scores").fetchone(), (70, 1))
        row = self.conn.execute("SELECT fit_score,is_stale,deterministic_json FROM scores").fetchone()
        self.assertEqual(row[0:2], (75, 0))
        self.assertIn('"score": 75', row[2])


if __name__ == "__main__":
    unittest.main()
