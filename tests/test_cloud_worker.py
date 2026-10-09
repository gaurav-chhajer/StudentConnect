import os
import smtplib
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import requests

from backend import automatic_matching, cloud_worker, pipeline_skeleton as pipeline
from backend.app import create_app
from scripts import run_cloud_worker as runner
from tests import test_evaluation, test_web_flows

KEY = "cd" * 32  # Synthetic test credential only.
HEADERS = {"Authorization": "Bearer " + KEY}
NOW = datetime(2026, 10, 8, 2, 30, tzinfo=timezone.utc)


class CloudEndpointTests(unittest.TestCase):
    def setUp(self):
        test_web_flows.WebFlowTests.setUp(self)
        test_web_flows.WebFlowTests.seed_user(self)
        self.conn.execute("UPDATE scores SET is_stale=1")
        self.conn.commit()
        env = patch.dict(os.environ, STUDENTCONNECT_WORKER_KEY=KEY)
        env.start()
        self.addCleanup(env.stop)
        self.client = self.app.test_client()  # No browser session or CSRF token.

    def call(self, route, data=None):
        if data is None:
            response = self.client.get("/internal/worker/" + route, headers=HEADERS)
        else:
            response = self.client.post("/internal/worker/" + route, headers=HEADERS, json=data)
        self.addCleanup(response.close)
        self.addCleanup(response.request.input_stream.close)
        return response

    def item(self):
        return self.call("work").json["items"][0]

    def score_data(self, item):
        return dict(user_id=item["user"]["id"], job_id=item["job"]["id"],
                    fingerprint=item["fingerprint"], result=test_evaluation.sample_result())

    def test_requires_separate_auth_and_keeps_browser_csrf(self):
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            self.assertEqual(self.client.get("/internal/worker/work", headers=headers).status_code, 401)
        with patch.dict(os.environ, STUDENTCONNECT_WORKER_KEY=""):
            self.assertEqual(self.call("work").status_code, 401)
        self.client.set_cookie("session", "synthetic")
        self.assertEqual(self.call("work").status_code, 401)
        self.assertEqual(self.client.post("/settings", headers=HEADERS, json={}).status_code, 401)

    def test_work_is_private_filtered_and_paginated(self):
        payload = self.call("work").json
        self.assertEqual(payload["next"], 1)
        self.assertEqual(set(payload["items"][0]["user"]), {"id", "resume_text", "preferences_json"})
        for secret_field in ("password_hash", "email", "name", "session_version"):
            self.assertNotIn(secret_field, payload["items"][0]["user"])
        self.assertEqual(self.call("work?after=1").json, dict(items=[], next=None))
        self.assertEqual(self.call("work?after=-1").status_code, 400)

    def test_score_validation_deduplication_and_changed_resume(self):
        data = self.score_data(self.item())
        self.assertEqual(self.call("score", dict(data, result={})).status_code, 400)
        self.assertEqual(self.call("score", data).json, {"saved": True})
        self.assertTrue(self.call("score", data).json["already_scored"])
        item = self.item()
        self.conn.execute("UPDATE users SET resume_text='Changed resume'")
        self.conn.commit()
        self.assertEqual(self.call("score", self.score_data(item)).status_code, 409)

    def test_score_rejects_unsubscribed_and_changed_posting(self):
        item = self.item()
        self.conn.execute("UPDATE jobs SET raw_json='{}' WHERE id=?", (item["job"]["id"],))
        self.conn.commit()
        self.assertEqual(self.call("score", self.score_data(item)).status_code, 409)
        item = self.call("work").json["items"][1]
        self.conn.execute("INSERT INTO user_settings(user_id,subscriptions_configured) VALUES(1,1)")
        self.conn.commit()
        self.assertEqual(self.call("score", self.score_data(item)).status_code, 409)

    def test_complete_snapshot_empty_snapshot_and_invalid_snapshot(self):
        company = self.call("companies").json["companies"][0]
        data = dict(company_id=1, fingerprint=company["fingerprint"], jobs=[dict(
            external_id="new", title="Software Engineer", url="https://example.test/apply",
            raw_json={"descriptionPlain": "Python"})])
        self.assertEqual(self.call("snapshot", data).json, {"saved": 1})
        self.assertEqual(self.conn.execute("SELECT is_active FROM jobs WHERE id=1").fetchone()[0], 0)
        self.assertEqual(self.call("snapshot", dict(data, jobs=[{}])).status_code, 400)
        self.assertEqual(self.call("snapshot", dict(data, jobs=[])).json, {"saved": 0})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM jobs WHERE company_id=1 AND is_active=1").fetchone()[0], 0)
        self.conn.execute("UPDATE companies SET board_slug='changed' WHERE id=1")
        self.conn.commit()
        self.assertEqual(self.call("snapshot", data).status_code, 409)

    def test_bounded_and_invalid_input(self):
        for data in ({"after": True}, {"after": -1}, {"after": "0"}):
            self.assertEqual(self.call("email", data).status_code, 400)
        self.assertEqual(self.call("snapshot", {"company_id": True, "jobs": []}).status_code, 400)
        response = self.client.post("/internal/worker/score", headers=HEADERS,
                                    data=b"x" * (cloud_worker.MAX_BODY_BYTES + 1))
        self.assertEqual(response.status_code, 413)

    def test_send_acceptance_and_repeat_deduplication(self):
        self.conn.execute("UPDATE scores SET is_stale=0")
        self.conn.execute("UPDATE jobs SET last_seen_at=?", (NOW.isoformat(),))
        self.conn.commit()
        with patch.object(cloud_worker, "datetime") as clock, \
                patch.object(pipeline, "validate_email_configuration"), \
                patch.object(pipeline, "deliver_email") as mail:
            clock.now.return_value = NOW
            clock.fromisoformat.side_effect = datetime.fromisoformat
            self.assertEqual(self.call("email", {"after": 0}).json, dict(next=1, sent=True))
            self.assertEqual(self.call("email", {"after": 0}).json, dict(next=1, sent=False))
            self.assertEqual(self.call("email", {"after": 1}).json, dict(next=None, sent=False))
        mail.assert_called_once()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sent_log").fetchone()[0], 2)
        self.assertFalse((self.database.parent / "email_schedule.paused").exists())

    def test_uncertain_send_pauses_and_is_not_retried(self):
        self.conn.execute("UPDATE scores SET is_stale=0")
        self.conn.execute("UPDATE jobs SET last_seen_at=?", (datetime.now(timezone.utc).isoformat(),))
        self.conn.commit()
        with patch.object(cloud_worker, "fixed_schedule_due", return_value=True), \
                patch.object(pipeline, "validate_email_configuration"), \
                patch.object(pipeline, "deliver_email", side_effect=smtplib.SMTPServerDisconnected("private")) as mail:
            self.assertEqual(self.call("email", {"after": 0}).status_code, 503)
            self.assertEqual(self.call("email", {"after": 0}).status_code, 503)
        mail.assert_called_once()
        self.assertTrue((self.database.parent / "email_schedule.paused").exists())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sent_log").fetchone()[0], 0)

    def test_fixed_time_off_weekly_and_legacy_time_preserved(self):
        self.conn.execute("INSERT INTO user_settings(user_id,cadence,send_time,timezone) VALUES(1,'daily','23:00','UTC')")
        self.conn.commit()
        self.assertFalse(cloud_worker.fixed_schedule_due(self.conn, 1, NOW.replace(hour=2, minute=29)))
        self.assertTrue(cloud_worker.fixed_schedule_due(self.conn, 1, NOW))
        self.conn.execute("UPDATE user_settings SET cadence='off'")
        self.assertFalse(cloud_worker.fixed_schedule_due(self.conn, 1, NOW))
        self.conn.execute("UPDATE user_settings SET cadence='weekly'")
        self.conn.execute("INSERT INTO sent_log(user_id,job_id,channel,sent_at) VALUES(1,1,'email','2026-10-02T02:30:00+00:00')")
        self.assertFalse(cloud_worker.fixed_schedule_due(self.conn, 1, NOW))
        self.assertTrue(cloud_worker.fixed_schedule_due(self.conn, 1, NOW.replace(day=9)))
        self.assertEqual(self.conn.execute("SELECT send_time FROM user_settings").fetchone()[0], "23:00")

    def test_cloud_key_stops_local_background_worker(self):
        with patch.object(automatic_matching, "MatchingService") as service:
            automatic_matching.ensure_started(self.database)
        service.assert_not_called()
        with patch.dict(os.environ, STUDENTCONNECT_WORKER_KEY="invalid"):
            with self.assertRaises(ValueError):
                create_app(self.database, self.uploads)

    def test_full_remote_fetch_score_and_email_flow(self):
        client = MagicMock()
        def hosted_call(route, data=None):
            response = self.call(route, data)
            self.assertEqual(response.status_code, 200, response.json)
            return response.json
        client.call.side_effect = hosted_call
        def fetched(company):
            return [dict(external_id=str(company["id"]), title="Software Engineer",
                         url="https://example.test/apply", raw_json={"descriptionPlain": "Python"})]
        with patch.object(pipeline, "GROQ_API_KEY", "synthetic"), \
                patch.object(pipeline, "fetch_jobs_for_company", side_effect=fetched), \
                patch.object(pipeline, "score_job_for_user", return_value=test_evaluation.sample_result()) as score, \
                patch.object(runner.time, "monotonic", return_value=0):
            self.assertEqual(runner.fetch_and_score(client, 2100), 0)
        self.assertEqual(score.call_count, 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM scores WHERE is_stale=0").fetchone()[0], 2)
        with patch.object(cloud_worker, "fixed_schedule_due", return_value=True), \
                patch.object(pipeline, "validate_email_configuration"), \
                patch.object(pipeline, "deliver_email") as mail, \
                patch.object(runner.time, "monotonic", return_value=0):
            self.assertEqual(runner.send_emails(client, 420), 0)
            self.assertEqual(runner.send_emails(client, 420), 0)
        mail.assert_called_once()

    def test_failed_refresh_excluded_from_email(self):
        self.conn.execute("UPDATE scores SET is_stale=0")
        self.conn.execute("UPDATE jobs SET last_seen_at=? WHERE company_id=1", (NOW.isoformat(),))
        self.conn.commit()
        with patch.object(cloud_worker, "datetime") as clock, \
                patch.object(pipeline, "validate_email_configuration"), \
                patch.object(pipeline, "deliver_email") as mail:
            clock.now.return_value = NOW
            self.assertTrue(self.call("email", {"after": 0}).json["sent"])
        self.assertEqual([match["job_id"] for match in mail.call_args.args[1]], [1])

    def test_lock_contention_and_invalid_evidence_do_not_write(self):
        data = self.score_data(self.item())
        data["result"]["matched_requirements"] = [dict(requirement="Python", jd_evidence="Invented", resume_evidence="Python")]
        self.assertEqual(self.call("score", data).status_code, 400)
        with cloud_worker.run_pipeline.exclusive_cycle(self.database.parent / "pipeline.lock"):
            self.assertEqual(self.call("score", data).status_code, 503)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM scores WHERE is_stale=0").fetchone()[0], 0)

    def test_fixed_schedule_is_visible_in_settings(self):
        with self.client.session_transaction() as session:
            session["user_id"], session["session_version"] = 1, 0
        response = self.client.get("/settings")
        self.assertIn(b"8:00 AM India time", response.data)
        self.assertNotIn(b'type="time"', response.data)


class CloudRunnerTests(unittest.TestCase):
    def test_board_diagnostics_identify_failure_without_private_payload(self):
        client = MagicMock()
        companies = [dict(id=number, name="Synthetic", ats_type="lever", board_slug="synthetic", fingerprint="0" * 64)
                     for number in (1, 2, 3)]
        response = requests.Response()
        response.status_code = 404
        error = requests.HTTPError("private response and credentials", response=response)
        client.call.side_effect = [dict(companies=companies), ValueError("private snapshot"), dict(saved=0), dict(items=[], next=None)]
        with patch.object(pipeline, "GROQ_API_KEY", "synthetic"), \
                patch.object(pipeline, "fetch_jobs_for_company", side_effect=[error, [], []]), \
                patch.object(runner.time, "monotonic", return_value=0), \
                self.assertLogs(runner.logger, level="INFO") as logs:
            self.assertEqual(runner.fetch_and_score(client, 2100), 1)
        output = " ".join(logs.output)
        self.assertIn("company_id=1 stage=fetch error=HTTPError http_status=404", output)
        self.assertIn("company_id=2 stage=snapshot error=ValueError", output)
        self.assertIn("boards_succeeded=1 boards_total=3 partial_failure=True", output)
        self.assertIn("eligible_accounts=0 evaluations_attempted=0", output)
        self.assertNotIn("private", output)
        self.assertNotIn("credentials", output)

    def test_evening_fetch_needs_no_groq_and_never_scores_or_emails(self):
        client = MagicMock()
        companies = [dict(id=1, name="Synthetic", ats_type="lever", board_slug="synthetic", fingerprint="0" * 64)]
        client.call.side_effect = [dict(companies=companies), dict(saved=0)]
        with patch.object(pipeline, "GROQ_API_KEY", ""), \
                patch.object(pipeline, "fetch_jobs_for_company", return_value=[]), \
                patch.object(pipeline, "score_job_for_user") as score, \
                patch.object(runner, "send_emails") as emails, \
                patch.object(runner, "HostedClient", return_value=client), \
                patch.object(runner.time, "monotonic", return_value=0):
            self.assertEqual(runner.main(["fetch"]), 0)
        self.assertEqual([call.args[0] for call in client.call.call_args_list], ["companies", "snapshot"])
        score.assert_not_called()
        emails.assert_not_called()
        client.close.assert_called_once()

    def test_evening_partial_failure_reports_failure_without_scoring(self):
        client = MagicMock()
        companies = [dict(id=number, name="Synthetic", ats_type="lever", board_slug="synthetic", fingerprint="0" * 64)
                     for number in (1, 2)]
        client.call.side_effect = [dict(companies=companies), dict(saved=0)]
        with patch.object(pipeline, "GROQ_API_KEY", ""), \
                patch.object(pipeline, "fetch_jobs_for_company", side_effect=[requests.Timeout(), []]), \
                patch.object(pipeline, "score_job_for_user") as score, \
                patch.object(runner.time, "monotonic", return_value=0):
            self.assertEqual(runner.fetch_and_score(client, 420, fetch_only=True), 1)
        score.assert_not_called()
        self.assertEqual(client.call.call_args.args[1]["company_id"], 2)

    def test_evening_budget_defers_remaining_boards(self):
        client = MagicMock()
        client.call.return_value = dict(companies=[{}])
        with patch.object(pipeline, "fetch_jobs_for_company") as fetch, \
                patch.object(runner.time, "monotonic", return_value=300):
            self.assertEqual(runner.fetch_and_score(client, 420, fetch_only=True), 1)
        fetch.assert_not_called()

    def test_partial_fetch_failure_and_no_successful_boards(self):
        client = MagicMock()
        companies = [dict(id=number, name="Synthetic", ats_type="lever", board_slug="synthetic", fingerprint="0" * 64)
                     for number in (1, 2)]
        client.call.side_effect = [dict(companies=companies), dict(saved=0), dict(items=[], next=None)]
        with patch.object(pipeline, "GROQ_API_KEY", "synthetic"), \
                patch.object(pipeline, "fetch_jobs_for_company", side_effect=[requests.Timeout(), []]), \
                patch.object(runner.time, "monotonic", return_value=0):
            self.assertEqual(runner.fetch_and_score(client, 2100), 1)
        self.assertEqual(client.call.call_args_list[1].args[1]["company_id"], 2)
        client.call.side_effect = None
        client.call.return_value = dict(companies=companies)
        with patch.object(pipeline, "GROQ_API_KEY", "synthetic"), \
                patch.object(pipeline, "fetch_jobs_for_company", side_effect=requests.Timeout()), \
                patch.object(runner.time, "monotonic", return_value=0):
            with self.assertRaises(RuntimeError):
                runner.fetch_and_score(client, 2100)

    def test_matching_time_budget_makes_no_board_calls(self):
        client = MagicMock()
        client.call.return_value = dict(companies=[{}])
        with patch.object(pipeline, "GROQ_API_KEY", "synthetic"), \
                patch.object(pipeline, "fetch_jobs_for_company") as fetch, \
                patch.object(runner.time, "monotonic", return_value=2100):
            self.assertEqual(runner.fetch_and_score(client, 2100), 1)
        fetch.assert_not_called()

    def test_email_pagination_budget_and_failure(self):
        client = MagicMock()
        client.call.side_effect = [dict(next=1, sent=True), dict(next=None, sent=False)]
        with patch.object(runner.time, "monotonic", return_value=0):
            self.assertEqual(runner.send_emails(client, 420), 0)
        self.assertEqual(client.call.call_args.args, ("email", {"after": 1}))
        client.reset_mock()
        with patch.object(runner.time, "monotonic", return_value=400):
            self.assertEqual(runner.send_emails(client, 420), 1)
        client.call.assert_not_called()
        client.call.side_effect = requests.Timeout("private")
        with patch.object(runner, "HostedClient", return_value=client), self.assertLogs(runner.logger, level="ERROR") as logs:
            self.assertEqual(runner.main(["email"]), 1)
        client.close.assert_called_once()
        self.assertNotIn("private", " ".join(logs.output))

    def test_cursor_and_configuration_validation(self):
        for value in (True, 0, -1, "1"):
            with self.assertRaises(ValueError):
                runner.cursor(value, 0)
        with patch.dict(os.environ, STUDENTCONNECT_WORKER_KEY="", STUDENTCONNECT_BASE_URL=""):
            with self.assertRaises(ValueError):
                runner.HostedClient()

    def test_http_limits_redirects_and_cleanup(self):
        with patch.dict(os.environ, STUDENTCONNECT_WORKER_KEY=KEY,
                        STUDENTCONNECT_BASE_URL="https://example.test"), patch.object(runner.requests, "Session") as session:
            client = runner.HostedClient()
            response = session.return_value.request.return_value.__enter__.return_value
            response.status_code = 200
            response.iter_content.return_value = [b'{"next":null}']
            self.assertEqual(client.call("email", {"after": 0}), {"next": None})
            self.assertFalse(session.return_value.request.call_args.kwargs["allow_redirects"])
            response.status_code = 302
            with self.assertRaises(RuntimeError):
                client.call("companies")
            response.status_code = 200
            response.iter_content.return_value = [b"x" * (cloud_worker.MAX_BODY_BYTES + 1)]
            with self.assertRaises(ValueError):
                client.call("companies")
            client.close()
            session.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
