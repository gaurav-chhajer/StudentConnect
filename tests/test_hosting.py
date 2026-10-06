import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from backend import accounts, hosting
from backend.app import create_app
from tests import test_web_flows

ORIGIN = "https://gauravchhajer.pythonanywhere.com"
ENV = {"STUDENTCONNECT_BASE_URL": ORIGIN, "STUDENTCONNECT_SECRET_KEY": "ab" * 32}


class HostingConfigurationTests(unittest.TestCase):
    def test_local_mode_and_public_configuration(self):
        with patch.dict(os.environ, {"STUDENTCONNECT_BASE_URL": "", "STUDENTCONNECT_SECRET_KEY": ""}):
            origin, hosted, key = hosting.application_settings()
            self.assertEqual(origin, "http://127.0.0.1:5000")
            self.assertFalse(hosted)
            self.assertEqual(len(key), 32)
        with patch.dict(os.environ, ENV):
            self.assertEqual(hosting.application_settings(), (ORIGIN, True, ENV["STUDENTCONNECT_SECRET_KEY"]))

    def test_invalid_hosted_configuration_fails(self):
        for origin in ("http://example.test", "https://user@example.test", "https://example.test/path",
                       "https://example.test:443", "https://example.test?x=1", "https://example.test#fragment",
                       "https://example..test", "https://example.test\n"):
            with self.subTest(origin=origin), patch.dict(os.environ, dict(ENV, STUDENTCONNECT_BASE_URL=origin)):
                with self.assertRaises(ValueError):
                    hosting.application_settings()
        for key in ("", "short", "z" * 64):
            with patch.dict(os.environ, dict(ENV, STUDENTCONNECT_SECRET_KEY=key)):
                with self.assertRaises(ValueError):
                    hosting.application_settings()

    def test_private_loader_preserves_existing_variables(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / ".env"
            with self.assertRaises(ValueError):
                hosting.load_hosted_environment(path)
            path.write_text("SC_TEST_FILE=loaded\nSC_TEST_EXISTING=file\n", encoding="utf-8")
            path.chmod(0o600)
            with patch.dict(os.environ, {"SC_TEST_EXISTING": "process", "PYTHON_DOTENV_DISABLED": "0"}):
                hosting.load_hosted_environment(path)
                self.assertEqual(os.environ["SC_TEST_EXISTING"], "process")
                self.assertEqual(os.environ["SC_TEST_FILE"], "loaded")
            path.write_text("", encoding="utf-8")
            with self.assertRaises(ValueError):
                hosting.load_hosted_environment(path)
            path.write_text("x" * 65537, encoding="utf-8")
            with self.assertRaises(ValueError):
                hosting.load_hosted_environment(path)
            if os.name == "posix":
                path.write_text("SC_TEST_FILE=loaded", encoding="utf-8")
                path.chmod(0o644)
                with self.assertRaises(ValueError):
                    hosting.load_hosted_environment(path)


class HostedApplicationTests(unittest.TestCase):
    def setUp(self):
        test_web_flows.WebFlowTests.setUp(self)

    def test_exact_hostname_secure_cookie_and_shared_signing_key(self):
        with patch.dict(os.environ, ENV):
            first = create_app(self.database, self.uploads)
            second = create_app(self.database, self.uploads)
        first.testing = second.testing = True
        client = first.test_client()
        response = client.get("/login", base_url=ORIGIN)
        self.assertEqual(response.status_code, 200)
        cookie = response.headers["Set-Cookie"]
        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        for host in ("https://evil.test", ORIGIN + ".evil.test", "http://localhost"):
            self.assertEqual(client.get("/login", base_url=host).status_code, 403)
        signed = first.session_interface.get_signing_serializer(first).dumps({"csrf": "synthetic"})
        self.assertEqual(second.session_interface.get_signing_serializer(second).loads(signed), {"csrf": "synthetic"})
        self.assertEqual(client.get("/settings", base_url=ORIGIN).status_code, 302)
        self.assertEqual(client.post("/register", base_url=ORIGIN).status_code, 403)

    def test_account_emails_contain_full_urls_without_code_in_url(self):
        with patch.dict(os.environ, ENV), patch.object(accounts.pipeline, "validate_email_configuration"), \
                patch.object(accounts.pipeline, "deliver_account_email") as mail:
            now = datetime(2026, 10, 6, tzinfo=timezone.utc)
            accounts.issue_invitation(self.conn, "tester@example.test", now)
            self.assertIn(ORIGIN + "/register and", mail.call_args.args[2])
            self.assertNotIn("?", mail.call_args.args[2])
            test_web_flows.WebFlowTests.seed_user(self)
            accounts.issue_recovery(self.conn, "owner@example.test", now)
            self.assertIn(ORIGIN + "/reset-password and", mail.call_args.args[2])
            self.assertNotIn("?", mail.call_args.args[2])
