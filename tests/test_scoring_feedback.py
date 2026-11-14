import unittest
from pathlib import Path
from unittest.mock import patch

from backend import scoring_feedback
from tests.test_web_flows import WebFlowTests


class ScoreFeedbackTests(unittest.TestCase):
    seed_user = WebFlowTests.seed_user
    post = WebFlowTests.post

    def setUp(self):
        WebFlowTests.setUp(self)
        self.seed_user()

    def signature(self, job_id=1):
        row = self.conn.execute("""SELECT s.id,s.fit_score,s.reasoning,s.matched_skills,s.missing_requirements,
                s.evaluation_json,s.deterministic_json,s.scored_at FROM scores s
                WHERE s.user_id=1 AND s.job_id=? AND s.is_stale=0""", (job_id,)).fetchone()
        return scoring_feedback.score_signature(row)

    def test_jobs_page_shows_feedback_for_selected_saved_score(self):
        page = self.client.get("/jobs?job=1")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Was this score accurate?", page.data)
        self.assertIn(b'name="score_signature"', page.data)

    def test_owner_can_review_aggregated_score_ratings(self):
        response = self.post("/jobs/score-feedback", dict(job_id="1", rating="1",
                                                            score_signature=self.signature()))
        self.assertEqual(response.status_code, 302)
        self.conn.execute("UPDATE accounts SET is_admin=1 WHERE user_id=1")
        self.conn.commit()
        page = self.client.get("/admin/feedback")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Score quality ratings", page.data)
        self.assertIn(b"1 helpful", page.data)
        self.assertIn(b"Software Engineer 1", page.data)

    def test_user_can_rate_current_score_and_update_rating(self):
        signature = self.signature()
        for rating in (1, -1):
            response = self.post("/jobs/score-feedback", dict(job_id="1", rating=str(rating),
                                                                 score_signature=signature))
            self.assertEqual(response.status_code, 302)
        rows = self.conn.execute("SELECT user_id,job_id,score_signature,rating FROM score_feedback").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["user_id"], rows[0]["job_id"], rows[0]["rating"]), (1, 1, -1))
        page = self.client.get("/jobs?job=1")
        self.assertIn(b"Your current rating: Not accurate", page.data)

    def test_stale_score_signature_is_rejected(self):
        signature = self.signature()
        self.conn.execute("UPDATE scores SET fit_score=84 WHERE user_id=1 AND job_id=1")
        self.conn.commit()
        response = self.post("/jobs/score-feedback", dict(job_id="1", rating="1", score_signature=signature))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM score_feedback").fetchone()[0], 0)

    def test_invalid_rating_and_other_users_score_are_rejected(self):
        signature = self.signature()
        for form in (dict(job_id="1", rating="0", score_signature=signature),
                     dict(job_id="2", rating="1", score_signature=signature),
                     dict(job_id="1", rating="1", score_signature="x" * 64)):
            response = self.post("/jobs/score-feedback", form)
            self.assertEqual(response.status_code, 302)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM score_feedback").fetchone()[0], 0)

    def test_missing_csrf_token_does_not_store_feedback(self):
        response = self.client.post("/jobs/score-feedback", data={
            "job_id": "1", "rating": "1", "score_signature": self.signature()})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM score_feedback").fetchone()[0], 0)

    def test_account_deletion_removes_score_feedback(self):
        from backend.accounts import delete_user_data
        self.conn.execute("INSERT INTO users(id,name,email,resume_text) VALUES(2,'Other','other@example.test','resume')")
        self.conn.execute("INSERT INTO accounts(user_id,password_hash,created_at,is_admin) VALUES(2,'unused','now',0)")
        self.conn.execute("INSERT INTO scores(job_id,user_id,fit_score,reasoning,scored_at) VALUES(1,2,50,'legacy','now')")
        signature = self.signature()
        self.conn.execute("INSERT INTO score_feedback(user_id,job_id,score_signature,rating,created_at) VALUES(1,1,?,1,'now')", (signature,))
        self.conn.commit()
        cache_path = Path(self.directory.name) / "cache.sqlite"
        with patch("backend.local_embeddings.CACHE_PATH", cache_path):
            delete_user_data(self.conn, 1, self.uploads)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM score_feedback WHERE user_id=1").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
