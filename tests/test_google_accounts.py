"""Offline Google signup, identity linking, and callback security checks."""

import os
import re
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from requests.exceptions import Timeout

from backend import accounts, google_accounts
from backend.app import create_app
from tests.test_web_flows import WebFlowTests

NOW = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
ENV = {"STUDENTCONNECT_GOOGLE_CLIENT_ID": "synthetic.apps.googleusercontent.com",
       "STUDENTCONNECT_GOOGLE_CLIENT_SECRET": "synthetic-client-secret"}
CLAIMS = {"sub": "123456789", "email": "student@example.test", "email_verified": True, "name": "Student"}
METADATA = {"issuer": "https://accounts.google.com",
            "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_endpoint": "https://oauth2.googleapis.com/token",
            "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
            "id_token_signing_alg_values_supported": ["RS256"], "_loaded_at": 1}


class GoogleAccountTests(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ, ENV):
            WebFlowTests.setUp(self)
        clock = patch("backend.app.utc_now", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        self.google = self.app.extensions["authlib.integrations.flask_client"].create_client("google")
        self.google.server_metadata.update(METADATA)

    def start(self, client=None):
        client = client or self.client
        page = client.get("/login")
        csrf = re.search(rb'name="csrf_token" value="([^"]+)"', page.data).group(1).decode()
        response = client.post("/auth/google", data={"csrf_token": csrf})
        self.assertEqual(response.status_code, 302)
        params = parse_qs(urlsplit(response.location).query)
        return params["state"][0], params, response

    def callback(self, state, claims=None, client=None):
        with patch.object(self.google, "fetch_access_token", return_value={"id_token": "synthetic"}) as exchange, \
                patch.object(self.google, "parse_id_token", return_value=CLAIMS if claims is None else claims) as verify:
            response = (client or self.client).get(google_accounts.CALLBACK_PATH, query_string={"state": state, "code": "synthetic-code"})
        return response, exchange, verify

    def test_public_signup_and_returning_login_without_invitation(self):
        self.assertIn(b"Continue with Google", self.client.get("/register").data)
        state, params, start = self.start()
        self.assertEqual(params["scope"], ["openid email profile"])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        self.assertEqual(params["redirect_uri"], ["http://127.0.0.1:5000/auth/google/callback"])
        self.assertIn("SameSite=Lax", start.headers["Set-Cookie"])
        self.assertIn("HttpOnly", start.headers["Set-Cookie"])
        pending = self.conn.execute("SELECT * FROM google_login_states").fetchone()
        response, exchange, verify = self.callback(state)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Google sign-in complete", response.data)
        self.assertIn(b'href="/"', response.data)
        self.assertIn("SameSite=Strict", " ".join(response.headers.getlist("Set-Cookie")))
        exchange.assert_called_once_with(redirect_uri=params["redirect_uri"][0], code="synthetic-code", code_verifier=pending["code_verifier"])
        verify.assert_called_once_with({"id_token": "synthetic"}, nonce=params["nonce"][0], leeway=30)
        account = self.conn.execute("SELECT * FROM accounts").fetchone()
        self.assertEqual((account["is_admin"], account["email_verified"]), (0, 1))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM account_tokens").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT subscriptions_configured FROM user_settings").fetchone()[0], 1)
        self.assertEqual(self.client.get("/settings").status_code, 200)
        second = self.app.test_client()
        state, _, _ = self.start(second)
        self.assertEqual(self.callback(state, client=second)[0].status_code, 302)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)

    def test_csrf_browser_binding_expiry_and_replay(self):
        self.assertEqual(self.client.post("/auth/google").status_code, 403)
        state, _, _ = self.start()
        stranger = self.app.test_client()
        response, exchange, _ = self.callback(state, client=stranger)
        self.assertEqual(response.status_code, 400)
        exchange.assert_not_called()
        self.assertEqual(self.callback(state)[0].status_code, 200)
        response, exchange, _ = self.callback(state)
        self.assertEqual(response.status_code, 400)
        exchange.assert_not_called()
        state, _, _ = self.start()
        with patch("backend.app.utc_now", return_value=NOW + timedelta(seconds=300)):
            response, exchange, _ = self.callback(state)
        self.assertEqual(response.status_code, 400)
        exchange.assert_not_called()

    def test_invalid_claims_create_no_accounts(self):
        for overrides in ({"email_verified": False}, {"email_verified": "true"}, {"email": None},
                          {"email": "invalid"}, {"sub": ""}, {"name": None}, {"name": "x" * 101}):
            with self.subTest(overrides=overrides):
                state, _, _ = self.start()
                self.assertEqual(self.callback(state, dict(CLAIMS, **overrides))[0].status_code, 400)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)

    def test_existing_profile_requires_authenticated_link_and_preserves_data(self):
        WebFlowTests.seed_user(self)
        before = dict(self.conn.execute("SELECT * FROM users WHERE id=1").fetchone())
        claims = dict(CLAIMS, email="owner@example.test")
        anonymous = self.app.test_client()
        state, _, _ = self.start(anonymous)
        response, _, _ = self.callback(state, claims, anonymous)
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Sign in with your password first", response.data)
        state, _, _ = self.start()
        # Google redirects do not carry the Strict session cookie.
        self.client.delete_cookie("session")
        self.assertEqual(self.callback(state, claims)[0].status_code, 302)
        self.assertEqual(before, dict(self.conn.execute("SELECT * FROM users WHERE id=1").fetchone()))
        self.assertEqual(self.conn.execute("SELECT user_id FROM google_accounts").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT password_hash FROM accounts").fetchone()[0], "unused-test-fixture")

    def test_revoked_link_session_and_wrong_email_are_rejected(self):
        WebFlowTests.seed_user(self)
        state, _, _ = self.start()
        self.assertEqual(self.callback(state)[0].status_code, 400)
        state, _, _ = self.start()
        self.conn.execute("UPDATE accounts SET session_version=1")
        self.conn.commit()
        self.assertEqual(self.callback(state, dict(CLAIMS, email="owner@example.test"))[0].status_code, 400)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM google_accounts").fetchone()[0], 0)

    def test_disabled_unverified_locked_and_changed_email_profiles_are_rejected(self):
        google_accounts.google_account(self.conn, CLAIMS, NOW)
        for sql in ("UPDATE users SET active=0", "UPDATE accounts SET email_verified=0",
                    "UPDATE accounts SET locked_until=9999999999", "UPDATE users SET email='changed@example.test'"):
            with self.subTest(sql=sql):
                self.conn.execute(sql)
                self.conn.commit()
                with self.assertRaises(ValueError):
                    google_accounts.google_account(self.conn, CLAIMS, NOW)
                self.conn.execute("UPDATE users SET active=1,email='student@example.test'")
                self.conn.execute("UPDATE accounts SET email_verified=1,locked_until=0")
                self.conn.commit()

    def test_cancellation_and_dependency_failure_consume_state(self):
        state, _, _ = self.start()
        response = self.client.get(google_accounts.CALLBACK_PATH, query_string={"state": state, "error": "access_denied"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM google_login_states").fetchone()[0], 0)
        state, _, _ = self.start()
        with patch.object(self.google, "fetch_access_token", side_effect=Timeout("sensitive provider detail")), self.assertLogs(self.app.logger, level="ERROR") as logs:
            response = self.client.get(google_accounts.CALLBACK_PATH, query_string={"state": state, "code": "synthetic"})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(b"sensitive provider detail", response.data)
        self.assertNotIn("sensitive provider detail", " ".join(logs.output))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM google_login_states").fetchone()[0], 0)

    def test_missing_and_partial_configuration(self):
        with patch.dict(os.environ, {key: "" for key in ENV}):
            app = create_app(self.database, self.uploads)
        self.assertFalse(app.config["GOOGLE_LOGIN_ENABLED"])
        self.assertNotIn(b"Continue with Google", app.test_client().get("/register").data)
        with patch.dict(os.environ, dict(ENV, STUDENTCONNECT_GOOGLE_CLIENT_SECRET="")):
            with self.assertRaises(ValueError):
                create_app(self.database, self.uploads)
