import re
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from tests import test_web_flows

NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
VALID = dict(accuracy="4", ease="5", would_use="yes", issues="Upload was slow", feature_request="Dark mode")


class FeedbackTests(unittest.TestCase):
    seed_user = test_web_flows.WebFlowTests.seed_user

    def setUp(self):
        test_web_flows.WebFlowTests.setUp(self)
        clock = patch("backend.app.utc_now", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def submit(self, **overrides):
        data = dict(VALID, **overrides)
        data = {key: value for key, value in data.items() if value is not None}
        page = self.client.get("/feedback")
        token = re.search(rb'name="csrf_token" value="([^"]+)"', page.data)
        self.assertIsNotNone(token)
        response = self.client.post("/feedback", data=dict(data, csrf_token=token.group(1).decode()))
        self.addCleanup(response.close)
        self.addCleanup(response.request.input_stream.close)
        return response

    def test_signed_out_user_sees_feedback_form_and_account_links(self):
        page = self.client.get("/feedback")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Send feedback", page.data)
        self.assertIn(b"Sign in", page.data)
        self.assertIn(b"Sign up", page.data)
        self.assertNotIn(b"Set up your profile", page.data)
        self.assertEqual(self.submit().status_code, 302)
        row = self.conn.execute("SELECT user_id,anonymous_key FROM feedback").fetchone()
        self.assertIsNone(row["user_id"])
        self.assertTrue(row["anonymous_key"])

    def test_signed_out_homepage_explains_product_and_offers_account_access(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Find roles that fit your resume", response.data)
        self.assertIn(b"Sign in", response.data)
        self.assertIn(b"Sign up", response.data)
        self.assertNotIn(b"Send feedback", response.data)

    def test_signed_in_user_can_submit_and_is_saved(self):
        self.seed_user()
        page = self.client.get("/feedback")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Send feedback", page.data)
        response = self.submit()
        self.assertEqual(response.status_code, 302)
        row = self.conn.execute("SELECT * FROM feedback").fetchone()
        self.assertEqual((row["user_id"], row["accuracy"], row["ease"], row["would_use"]), (1, 4, 5, "yes"))
        self.assertEqual(row["issues"], "Upload was slow")

    def test_post_requires_csrf_token(self):
        self.seed_user()
        self.assertEqual(self.client.post("/feedback", data=VALID).status_code, 403)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0], 0)

    def test_invalid_input_is_rejected(self):
        self.seed_user()
        for bad in (dict(accuracy="0"), dict(accuracy="6"), dict(ease="x"), dict(accuracy=None),
                    dict(would_use="definitely"), dict(issues="x" * 2001)):
            self.assertEqual(self.submit(**bad).status_code, 400, bad)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0], 0)

    def test_daily_limit_is_enforced_per_user(self):
        self.seed_user()
        for _ in range(10):
            self.assertEqual(self.submit().status_code, 302)
        self.assertEqual(self.submit().status_code, 400)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0], 10)

    def test_anonymous_daily_limit_is_enforced_per_session(self):
        for _ in range(10):
            self.assertEqual(self.submit().status_code, 302)
        self.assertEqual(self.submit().status_code, 400)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0], 10)

    def test_text_is_escaped_on_the_admin_page(self):
        self.seed_user()
        self.conn.execute("UPDATE accounts SET is_admin=1 WHERE user_id=1")
        self.conn.commit()
        self.submit(issues="<script>alert(1)</script>")
        page = self.client.get("/admin/feedback")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn(b"<script>alert(1)</script>", page.data)
        self.assertIn(b"&lt;script&gt;alert(1)&lt;/script&gt;", page.data)

    def test_only_the_owner_can_read_feedback(self):
        self.seed_user()
        self.submit()
        self.assertEqual(self.client.get("/admin/feedback").status_code, 403)
        self.conn.execute("UPDATE accounts SET is_admin=1 WHERE user_id=1")
        self.conn.commit()
        page = self.client.get("/admin/feedback")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Upload was slow", page.data)


if __name__ == "__main__":
    unittest.main()
