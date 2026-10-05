import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from backend.app import create_app
from backend.pipeline_skeleton import SCHEMA
from backend.migrate import apply_migrations


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / "jobs.db"
        self.conn = sqlite3.connect(self.database)
        self.addCleanup(self.conn.close)
        self.conn.executescript(SCHEMA)
        apply_migrations(self.conn)
        self.application = create_app(self.database)
        self.application.testing = True
        self.client = self.application.test_client()
        clock = patch("backend.app.utc_now", return_value=datetime(2026, 10, 4, tzinfo=timezone.utc))
        clock.start()
        self.addCleanup(clock.stop)
        network = patch("backend.pipeline_skeleton.requests.post", side_effect=AssertionError("Unexpected external request"))
        network.start()
        self.addCleanup(network.stop)

    def seed(self):
        self.conn.execute("INSERT INTO users(id,name,email,resume_text) VALUES (1,?,?,?)", ("Student <script>alert(1)</script>", "one@example.test", "Private resume"))
        self.conn.execute("INSERT INTO companies(id,name,ats_type,board_slug) VALUES(1,'Example','lever','example')")
        for number, score, active in ((1, 85, 1), (2, 55, 1), (3, 99, 0)):
            self.conn.execute("""INSERT INTO jobs(id,company_id,external_id,title,location,url,first_seen_at,last_seen_at,is_active)
                VALUES(?,1,?,?,'Remote',?,'2026-10-01T00:00:00+00:00','2026-10-01T00:00:00+00:00',?)""",
                (number, str(number), f"Engineer {number}", "https://example.test/job" if number != 2 else "javascript:alert(1)", active))
            self.conn.execute("""INSERT INTO scores(job_id,user_id,fit_score,reasoning,matched_skills,missing_requirements,scored_at)
                VALUES(?,1,?,'Good fit','["Python"]','["SQL"]','2026-10-02T00:00:00+00:00')""", (number, score))
        self.conn.commit()

    def test_empty_profile(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"No active profile", response.data)

    def test_saved_matches_are_rendered_safely_without_writes(self):
        self.seed()
        before = self.database.read_bytes()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        for text in (b"Engineer 1", b"Engineer 2", b"85%", b"Python", b"SQL", b"70.0%"):
            self.assertIn(text, response.data)
        for text in (b"Engineer 3", b"Private resume", b"one@example.test", b"javascript:alert", b"<script>alert"):
            self.assertNotIn(text, response.data)
        self.assertIn(b"&lt;script&gt;", response.data)
        self.assertEqual(before, self.database.read_bytes())
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_filters_and_invalid_input(self):
        self.seed()
        response = self.client.get("/?min_score=60&q=Engineer")
        self.assertEqual(response.status_code, 200)
        # The low score is absent from the match table and email candidates.
        self.assertNotIn(b"Engineer 2", response.data)
        for query in ("min_score=101", "min_score=oops", "page=0", "company=-1", "q=" + "a" * 201):
            with self.subTest(query=query):
                self.assertEqual(self.client.get("/?" + query).status_code, 400)

    def test_multiple_profiles_refuse_selection(self):
        self.seed()
        self.conn.execute("INSERT INTO users(name,email,resume_text) VALUES('Other','two@example.test','Secret')")
        self.conn.commit()
        response = self.client.get("/?user_id=2")
        self.assertEqual(response.status_code, 409)
        self.assertNotIn(b"Engineer 1", response.data)

    def test_scores_for_other_users_are_not_exposed(self):
        self.seed()
        self.conn.execute("INSERT INTO users(id,name,email,resume_text,active) VALUES(2,'Other','two@example.test','Secret',0)")
        self.conn.execute("""INSERT INTO scores(job_id,user_id,fit_score,reasoning,scored_at)
            VALUES(1,2,12,'Other user reasoning','2026-10-02T00:00:00+00:00')""")
        self.conn.commit()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"Other user reasoning", response.data)

    def test_missing_database_and_missing_schema(self):
        response = create_app(Path(self.directory.name) / "absent.db").test_client().get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"No job database", response.data)
        self.conn.execute("DROP TABLE scores")
        self.seed_user_only()
        with self.assertLogs(self.application.logger, level="ERROR"):
            self.assertEqual(self.client.get("/").status_code, 503)

    def seed_user_only(self):
        self.conn.execute("INSERT INTO users(name,email,resume_text) VALUES('Student','one@example.test','Resume')")
        self.conn.commit()

    def test_invalid_saved_score_and_skill_list(self):
        self.seed()
        for assignment in ("fit_score=150", "fit_score=85,matched_skills='{}'"):
            self.conn.execute("UPDATE scores SET " + assignment + " WHERE job_id=1")
            self.conn.commit()
            with self.assertLogs(self.application.logger, level="ERROR"):
                response = self.client.get("/")
            self.assertEqual(response.status_code, 503)
            self.assertNotIn(b"Engineer 1", response.data)

    def test_pagination(self):
        self.seed()
        for number in range(4, 27):
            self.conn.execute("""INSERT INTO jobs(id,company_id,external_id,title,first_seen_at,last_seen_at)
                VALUES(?,1,?,?,'2026-10-01','2026-10-01')""", (number, str(number), f"Unique role {number}"))
            self.conn.execute("""INSERT INTO scores(job_id,user_id,fit_score,reasoning,scored_at)
                VALUES(?,1,50,'Match','2026-10-02')""", (number,))
        self.conn.commit()
        first = self.client.get("/")
        second = self.client.get("/?page=2")
        self.assertIn(b"Page 1 of 2", first.data)
        self.assertNotIn(b"Unique role 26", first.data)
        self.assertIn(b"Unique role 26", second.data)

    def test_writes_and_unrelated_files_are_not_served(self):
        self.assertEqual(self.client.post("/").status_code, 405)
        self.assertEqual(self.client.get("/jobs.db").status_code, 404)


if __name__ == "__main__":
    unittest.main()
