import re
import smtplib
import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch

from werkzeug.security import check_password_hash
from backend import accounts,pipeline_skeleton as pipeline
from scripts.run_local_email import send_digest
from tests import test_web_flows

NOW = datetime(2026,10,6,12,tzinfo=timezone.utc)
TEST_PASSWORD = "Synthetic test password 123"
ACCOUNT_DELIVERY = pipeline.deliver_account_email


class AccountTests(unittest.TestCase):
    setUp = test_web_flows.WebFlowTests.setUp
    seed_user = test_web_flows.WebFlowTests.seed_user

    def setUp(self):
        test_web_flows.WebFlowTests.setUp(self)
        clock=patch("backend.app.utc_now",return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        mail=patch.object(pipeline,"deliver_account_email")
        self.mail=mail.start()
        self.addCleanup(mail.stop)
        configuration=patch.object(pipeline,"validate_email_configuration")
        self.configuration=configuration.start()
        self.addCleanup(configuration.stop)
        token_numbers=iter(range(1,100))
        codes=patch.object(accounts.secrets,"token_urlsafe",side_effect=lambda size:f"{next(token_numbers):043d}")
        codes.start()
        self.addCleanup(codes.stop)

    def invitation(self,email="tester@example.test",owner=False):
        accounts.issue_invitation(self.conn,email,NOW,owner=owner)
        return self.mail.call_args.args[2].split("\n\n")[-1]

    def form(self,email,code):
        return dict(email=email,code=code,name="Tester",password=TEST_PASSWORD,confirmation=TEST_PASSWORD)

    def post(self,path,data,client=None):
        client=client or self.client
        response=client.get("/settings" if path=="/logout" else "/register" if path=="/register/code" else path)
        token=re.search(rb'name="csrf_token" value="([^"]+)"',response.data)
        self.assertIsNotNone(token)
        result=client.post(path,data=dict(data,csrf_token=token.group(1).decode()))
        self.addCleanup(response.close)
        self.addCleanup(result.close)
        self.addCleanup(result.request.input_stream.close)
        return result

    def login(self,email="tester@example.test",client=None):
        return self.post("/login",dict(email=email,password=TEST_PASSWORD),client)

    def test_invited_registration_hashes_password_and_consumes_code(self):
        code=self.invitation()
        saved=self.conn.execute("SELECT * FROM account_tokens").fetchone()
        self.assertNotEqual(saved["token_hash"],code)
        self.assertEqual(saved["expires_at"],int(NOW.timestamp())+48*3600)
        self.assertEqual(self.post("/register",self.form("TESTER@example.test",code)).status_code,302)
        account=self.conn.execute("SELECT * FROM accounts").fetchone()
        self.assertTrue(check_password_hash(account["password_hash"],TEST_PASSWORD))
        self.assertNotEqual(account["password_hash"],TEST_PASSWORD)
        self.assertEqual(account["email_verified"],1)
        self.assertEqual(account["is_admin"],0)
        self.assertEqual(self.post("/register",self.form("tester@example.test",code)).status_code,400)
        self.assertEqual(self.login().status_code,302)
        self.assertEqual(self.client.get("/").status_code,200)

    def test_public_email_signup_requires_emailed_code_and_can_sign_in(self):
        self.assertIn(b"Email me a code", self.client.get("/register").data)
        self.assertEqual(self.post("/register/code", {"email": "tester@example.test"}).status_code, 200)
        code = self.mail.call_args.args[2].split("\n\n")[-1]
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)
        self.assertEqual(self.post("/register", self.form("tester@example.test", code)).status_code, 302)
        self.assertEqual(self.login().status_code, 302)
        account = self.conn.execute("SELECT is_admin,email_verified FROM accounts").fetchone()
        self.assertEqual(tuple(account), (0, 1))
        self.assertEqual(self.post("/register", self.form("tester@example.test", code)).status_code, 400)

    def test_public_code_request_does_not_reveal_existing_account(self):
        code = self.invitation()
        self.assertEqual(self.post("/register", self.form("tester@example.test", code)).status_code, 302)
        sent = self.mail.call_count
        response = self.post("/register/code", {"email": "tester@example.test"})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"If this email is eligible", response.data)
        self.assertEqual(self.mail.call_count, sent)

    def test_public_signup_code_expires_without_creating_an_account(self):
        self.assertEqual(self.post("/register/code", {"email": "tester@example.test"}).status_code, 200)
        code = self.mail.call_args.args[2].split("\n\n")[-1]
        with self.assertRaises(ValueError):
            accounts.redeem_invitation(self.conn, self.form("tester@example.test", code), NOW + timedelta(minutes=30))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 0)

    def test_public_signup_reports_unavailable_mail_configuration(self):
        self.configuration.side_effect = ValueError("private configuration")
        with self.assertLogs(level="ERROR") as logs:
            response = self.post("/register/code", {"email": "tester@example.test"})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private configuration", " ".join(logs.output))
        self.mail.assert_not_called()

    def test_public_signup_does_not_retry_uncertain_mail(self):
        self.mail.side_effect = ValueError("private delivery detail")
        with self.assertLogs(level="ERROR") as logs:
            response = self.post("/register/code", {"email": "tester@example.test"})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Check your inbox", response.data)
        self.assertNotIn("private delivery detail", " ".join(logs.output))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM account_tokens WHERE purpose='invite'").fetchone()[0], 1)
        self.assertEqual(self.mail.call_count, 1)

    def test_public_member_does_not_block_first_owner_invitation(self):
        self.assertEqual(self.post("/register/code", {"email": "tester@example.test"}).status_code, 200)
        member_code = self.mail.call_args.args[2].split("\n\n")[-1]
        self.assertEqual(self.post("/register", self.form("tester@example.test", member_code)).status_code, 302)
        owner_code = self.invitation("owner@example.test", owner=True)
        self.assertEqual(self.post("/register", self.form("owner@example.test", owner_code)).status_code, 302)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM accounts WHERE is_admin=1").fetchone()[0], 1)

    def test_account_pages_load_local_assets_with_restricted_script_nonce(self):
        for route in ("/login", "/register", "/forgot-password", "/reset-password"):
            with self.subTest(route=route):
                response = self.client.get(route)
                self.assertEqual(response.status_code, 200)
                nonce = re.search(rb'<script defer nonce="([^"]+)"', response.data).group(1).decode()
                self.assertIn("script-src 'nonce-" + nonce + "'", response.headers["Content-Security-Policy"])
                self.assertIn("script-src-attr 'none'", response.headers["Content-Security-Policy"])
                self.assertIn(b'name="csrf_token"', response.data)
                self.assertNotIn(b"fonts.googleapis.com", response.data)
                assets = re.findall(rb'(?:href|src)="(/static/[^"]+)"', response.data)
                self.assertTrue(assets)
                for asset in assets:
                    with self.client.get(asset.decode()) as static_response:
                        self.assertEqual(static_response.status_code, 200)
        self.assertEqual(self.client.post("/login", data=dict(email="tester@example.test", password=TEST_PASSWORD)).status_code, 403)

    def test_registration_requires_matching_unexpired_invitation(self):
        code=self.invitation()
        for form in (self.form("other@example.test",code),self.form("tester@example.test","a"*43),
                     dict(self.form("tester@example.test",code),password="short"),
                     dict(self.form("tester@example.test",code),confirmation="different")):
            self.assertEqual(self.post("/register",form).status_code,400)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],0)
        self.conn.execute("UPDATE account_tokens SET expires_at=?",(int(NOW.timestamp()),))
        self.conn.commit()
        self.assertEqual(self.post("/register",self.form("tester@example.test",code)).status_code,400)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0],0)

    def test_owner_invitation_links_existing_profile_without_overwriting(self):
        self.conn.execute("INSERT INTO users(id,email,name,resume_text) VALUES(7,'Owner@example.test','Original','Private original resume')")
        self.conn.execute("INSERT INTO user_settings(user_id,min_score) VALUES(7,79)")
        self.conn.commit()
        code=self.invitation("owner@example.test",owner=True)
        before=dict(self.conn.execute("SELECT * FROM users WHERE id=7").fetchone())
        self.assertEqual(self.post("/register",self.form("owner@example.test",code)).status_code,302)
        self.assertEqual(before,dict(self.conn.execute("SELECT * FROM users WHERE id=7").fetchone()))
        self.assertEqual(self.conn.execute("SELECT min_score FROM user_settings WHERE user_id=7").fetchone()[0],79)
        self.assertEqual(self.conn.execute("SELECT is_admin FROM accounts WHERE user_id=7").fetchone()[0],1)
        with self.assertRaises(ValueError):
            self.invitation("second@example.test",owner=True)

    def test_only_owner_can_invite_and_csrf_is_required(self):
        code=self.invitation()
        self.post("/register",self.form("tester@example.test",code))
        self.login()
        self.assertEqual(self.client.get("/accounts/invitations").status_code,403)
        self.assertEqual(self.client.post("/logout").status_code,403)
        self.conn.execute("UPDATE accounts SET is_admin=1")
        self.conn.commit()
        self.assertEqual(self.client.get("/accounts/invitations").status_code,200)
        self.assertEqual(self.post("/accounts/invitations",dict(email="new@example.test")).status_code,302)
        self.assertEqual(self.mail.call_count,2)
        self.assertEqual(self.post("/accounts/invitations",dict(email="new@example.test")).status_code,400)
        self.assertEqual(self.mail.call_count,2)

    def test_login_failures_lock_account_and_generic_errors_hide_identity(self):
        code=self.invitation()
        self.post("/register",self.form("tester@example.test",code))
        errors=[]
        for email in ["missing@example.test"]+["tester@example.test"]*5:
            result=self.post("/login",dict(email=email,password="wrong password"))
            self.assertEqual(result.status_code,400)
            errors.append(b"Unable to sign in" in result.data)
        self.assertTrue(all(errors))
        self.assertEqual(self.login().status_code,400)
        with patch("backend.app.utc_now",return_value=NOW+timedelta(minutes=16)):
            self.assertEqual(self.login().status_code,302)

    def test_logout_revokes_other_sessions_and_blocks_anonymous_writes(self):
        code=self.invitation()
        self.post("/register",self.form("tester@example.test",code))
        self.login()
        second=self.app.test_client()
        self.login(client=second)
        self.assertEqual(self.post("/logout",{},self.client).status_code,302)
        self.assertEqual(self.client.get("/resume").status_code,302)
        self.assertEqual(second.get("/resume").status_code,302)
        self.assertEqual(second.post("/resume",data=dict(resume_text="overwrite",consent="on")).status_code,401)

    def test_recovery_is_generic_cooldown_and_reset_revokes_sessions(self):
        code=self.invitation()
        self.post("/register",self.form("tester@example.test",code))
        self.login()
        anonymous=self.app.test_client()
        self.mail.reset_mock()
        self.assertEqual(self.post("/forgot-password",dict(email="missing@example.test"),anonymous).status_code,200)
        self.mail.assert_not_called()
        self.assertEqual(self.post("/forgot-password",dict(email="tester@example.test"),anonymous).status_code,200)
        recovery=self.mail.call_args.args[2].split("\n\n")[-1]
        self.post("/forgot-password",dict(email="tester@example.test"),anonymous)
        self.mail.assert_called_once()
        changed=dict(email="tester@example.test",code=recovery,password="A different test password 123",confirmation="A different test password 123")
        self.assertEqual(self.post("/reset-password",changed,anonymous).status_code,302)
        self.assertEqual(self.client.get("/resume").status_code,302)
        self.assertEqual(self.post("/reset-password",changed,anonymous).status_code,400)
        self.assertEqual(self.login().status_code,400)
        self.assertEqual(self.post("/login",dict(email="tester@example.test",password=changed["password"])).status_code,302)

    def test_recovery_mail_failure_is_logged_without_disclosing_code(self):
        code=self.invitation()
        self.post("/register",self.form("tester@example.test",code))
        self.mail.side_effect=smtplib.SMTPServerDisconnected("private delivery detail")
        with self.assertLogs(self.app.logger,level="ERROR") as logs:
            response=self.post("/forgot-password",dict(email="tester@example.test"))
        self.assertEqual(response.status_code,200)
        self.assertNotIn("private delivery detail"," ".join(logs.output))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM account_tokens WHERE purpose='reset'").fetchone()[0],1)

    def test_global_limits_and_csrf_cover_account_forms(self):
        self.assertEqual(self.client.post("/register",data={}).status_code,403)
        for _ in range(50):
            self.assertTrue(accounts.request_allowed(self.conn,"login",NOW))
        self.assertFalse(accounts.request_allowed(self.conn,"login",NOW))
        self.assertEqual(self.post("/login",dict(email="missing@example.test",password="bad")).status_code,429)
        self.assertTrue(accounts.request_allowed(self.conn,"login",NOW+timedelta(minutes=5)))

    def test_missing_email_configuration_does_not_create_pending_invitation(self):
        self.configuration.side_effect=RuntimeError("Configuration missing")
        with self.assertRaises(RuntimeError):
            self.invitation(owner=True)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM account_tokens").fetchone()[0],0)
        self.mail.assert_not_called()

    def test_expired_recovery_and_wrong_email_do_not_change_password(self):
        code=self.invitation()
        accounts.redeem_invitation(self.conn,self.form("tester@example.test",code),NOW)
        accounts.issue_recovery(self.conn,"tester@example.test",NOW)
        recovery=self.mail.call_args.args[2].split("\n\n")[-1]
        form=self.form("tester@example.test",recovery)
        with self.assertRaises(ValueError):
            accounts.reset_password(self.conn,dict(form,email="other@example.test"),NOW)
        with self.assertRaises(ValueError):
            accounts.reset_password(self.conn,form,NOW+timedelta(minutes=30))
        self.assertEqual(self.conn.execute("SELECT session_version FROM accounts").fetchone()[0],0)

    def test_second_digest_failure_keeps_first_users_sent_records(self):
        self.seed_user()
        code=self.invitation()
        accounts.redeem_invitation(self.conn,self.form("tester@example.test",code),NOW)
        self.conn.execute("UPDATE user_settings SET subscriptions_configured=0 WHERE user_id=2")
        self.conn.commit()
        pipeline.store_score(self.conn,2,2,dict(fit_score=90,reasoning="Tester-only"))
        with patch.object(pipeline,"deliver_email",side_effect=[None,TimeoutError()]):
            with self.assertRaises(TimeoutError):
                send_digest(self.conn,send=True)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sent_log WHERE user_id=1").fetchone()[0],2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sent_log WHERE user_id=2").fetchone()[0],0)

    def test_owner_bootstrap_requires_explicit_send_confirmation(self):
        from scripts import invite_owner
        with patch("builtins.input",side_effect=["owner@example.test","CANCEL"]), \
                patch.object(accounts,"issue_invitation") as invite, \
                self.assertLogs(level="INFO"):
            self.assertEqual(invite_owner.main(),0)
        invite.assert_not_called()

    def test_account_message_uses_verified_smtp_without_logging_codes(self):
        with patch.object(pipeline,"GMAIL_APP_PASSWORD","abcdefghijklmnop"), \
                patch.object(pipeline,"DIGEST_FROM_EMAIL","sender@example.test"), \
                patch.object(pipeline.smtplib,"SMTP_SSL") as smtp:
            smtp.return_value.__enter__.return_value.send_message.return_value={}
            ACCOUNT_DELIVERY("tester@example.test","Account code","Synthetic secret code")
            message=smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
            self.assertIn("Synthetic secret code",message.get_content())
            self.assertEqual(message["To"],"tester@example.test")
            self.assertTrue(smtp.call_args.kwargs["context"].check_hostname)
            smtp.return_value.__exit__.assert_called_once()

    def test_inactive_and_unverified_accounts_cannot_sign_in(self):
        code=self.invitation()
        accounts.redeem_invitation(self.conn,self.form("tester@example.test",code),NOW)
        self.conn.execute("UPDATE users SET active=0")
        self.conn.commit()
        self.assertEqual(self.login().status_code,400)
        self.conn.execute("UPDATE users SET active=1")
        self.conn.execute("UPDATE accounts SET email_verified=0")
        self.conn.commit()
        self.assertEqual(self.login().status_code,400)
        self.conn.execute("UPDATE accounts SET email_verified=1")
        self.conn.commit()
        self.assertEqual(self.login().status_code,302)
        self.conn.execute("UPDATE users SET active=0")
        self.conn.commit()
        self.assertEqual(self.client.get("/settings").status_code,302)

    def test_cross_account_data_and_writes_are_isolated(self):
        self.seed_user()
        self.conn.execute("UPDATE accounts SET password_hash=? WHERE user_id=1",(accounts.password_hash(TEST_PASSWORD,TEST_PASSWORD),))
        self.conn.execute("INSERT INTO applications(id,user_id,title,company_name,status,notes,created_at,updated_at) VALUES(44,1,'Owner private role','Private employer','saved','Private notes',?,?)",(NOW.isoformat(),NOW.isoformat()))
        self.conn.execute("INSERT INTO saved_searches(user_id,name,filters_json,updated_at) VALUES(1,'Owner private search','{}',?)",(NOW.isoformat(),))
        self.conn.commit()
        code=self.invitation()
        tester=self.app.test_client()
        self.post("/register",self.form("tester@example.test",code),tester)
        self.login(client=tester)
        for route in ("/?user_id=1","/jobs?user_id=1","/resume?user_id=1","/applications?user_id=1","/analytics?user_id=1","/settings?user_id=1"):
            page=tester.get(route)
            self.assertEqual(page.status_code,200)
            for private in (b"Python resume",b"Owner private",b"Private notes",b"owner@example.test",b"Software Engineer 1"):
                self.assertNotIn(private,page.data)
        self.assertEqual(tester.get("/jobs?job=1").status_code,404)
        page=tester.get("/settings")
        csrf=re.search(rb'name="csrf_token" value="([^"]+)"',page.data).group(1).decode()
        self.assertEqual(tester.post("/applications",data=dict(csrf_token=csrf,application_id="44",status="offer")).status_code,400)
        self.assertEqual(tester.post("/resume",data=dict(csrf_token=csrf,user_id="1",resume_text="Tester own resume",consent="on")).status_code,302)
        self.assertEqual(self.conn.execute("SELECT resume_text FROM users WHERE id=1").fetchone()[0],"Python resume")
        self.assertEqual(self.conn.execute("SELECT resume_text FROM users WHERE id=2").fetchone()[0],"Tester own resume")
        self.assertEqual(self.conn.execute("SELECT status FROM applications WHERE id=44").fetchone()[0],"saved")
        bad=test_web_flows.WebFlowTests.settings_data(self,email="owner@example.test")
        self.assertEqual(tester.post("/settings",data=dict(bad,csrf_token=csrf)).status_code,400)

    def test_digest_sends_each_users_matches_to_their_own_account(self):
        self.seed_user()
        code=self.invitation()
        accounts.redeem_invitation(self.conn,self.form("tester@example.test",code),NOW)
        self.conn.execute("UPDATE user_settings SET subscriptions_configured=0 WHERE user_id=2")
        self.conn.commit()
        pipeline.store_score(self.conn,2,2,dict(fit_score=92,reasoning="Tester-only reasoning"))
        with patch.object(pipeline,"deliver_email") as mail:
            self.assertEqual(send_digest(self.conn,send=True),3)
        self.assertEqual(mail.call_count,2)
        self.assertEqual(mail.call_args_list[0].args[0]["id"],1)
        self.assertEqual(mail.call_args_list[1].args[0]["id"],2)
        self.assertEqual([row["reasoning"] for row in mail.call_args_list[1].args[1]],["Tester-only reasoning"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sent_log WHERE user_id=1").fetchone()[0],2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sent_log WHERE user_id=2").fetchone()[0],1)
