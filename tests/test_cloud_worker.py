import os
import json
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

    def test_successful_companies_filter_before_candidate_limit(self):
        original = self.conn.execute('SELECT * FROM jobs WHERE id=1').fetchone()
        for number in range(3, 9):
            self.conn.execute("""INSERT INTO jobs(company_id,external_id,title,url,location,raw_json,first_seen_at,last_seen_at)
                VALUES(1,?,?,?,?,?,?,?)""", (str(number), original['title'], original['url'],
                original['location'], original['raw_json'], original['first_seen_at'], original['last_seen_at']))
        self.conn.commit()
        # Put every failed-board candidate first to reproduce batch starvation.
        candidates = pipeline.filter_candidate_jobs(self.conn, dict(id=1, preferences_json='{}'))
        candidates.sort(key=lambda job: job['company_id'])
        with patch.object(pipeline, 'filter_candidate_jobs', return_value=candidates):
            self.assertEqual(len(self.call('work').json['items']), cloud_worker.MAX_WORK_ITEMS)
            payload = self.call('work?company_id=2').json
        self.assertEqual([item['job']['id'] for item in payload['items']], [2])
        self.assertEqual(payload['next'], 1)
        self.assertEqual(self.call('work?company_id=99').json, dict(items=[], next=1))
        self.assertEqual(len(self.call('work?company_id=1&company_id=2').json['items']), cloud_worker.MAX_WORK_ITEMS)
        for value in ('', '0', '-1', 'abc', '2147483648'):
            self.assertEqual(self.call('work?company_id=' + value).status_code, 400)
        oversized = 'work?' + '&'.join(['company_id=1'] * (cloud_worker.MAX_COMPANIES + 1))
        self.assertEqual(self.call(oversized).status_code, 400)

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

    def test_observed_mode_is_server_owned_and_empty_does_not_close(self):
        self.conn.execute("UPDATE companies SET career_source='wipro' WHERE id=1")
        self.conn.commit()
        company = self.call('companies').json['companies'][0]
        data = dict(company_id=1, fingerprint=company['fingerprint'], jobs=[])
        self.assertEqual(self.call('snapshot', dict(data, mode='snapshot')).status_code, 400)
        self.assertEqual(self.call('snapshot', dict(data, mode='observed')).json, {'saved': 0})
        self.assertEqual(self.call('snapshot', data).status_code, 200)
        self.assertEqual(self.conn.execute('SELECT is_active FROM jobs WHERE id=1').fetchone()[0], 1)
        posting = dict(external_id='observed', title='Engineer', url='https://example.test/apply', raw_json={})
        self.assertEqual(self.call('snapshot', dict(data, jobs=[posting, posting])).json, {'saved': 1})
        with self.assertLogs('backend.job_ingestion', level='WARNING'):
            self.assertEqual(self.call('snapshot', dict(data, jobs=[posting, dict(posting, title='Other')])).json, {'saved': 0})
        self.assertEqual(self.conn.execute("SELECT title FROM jobs WHERE external_id='observed'").fetchone()[0], 'Engineer')

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

    def test_partial_refresh_scores_healthy_board_and_preserves_old_jobs(self):
        original = dict(self.conn.execute('SELECT * FROM jobs WHERE id=1').fetchone())
        for number in range(3, 9):
            self.conn.execute("""INSERT INTO jobs(company_id,external_id,title,url,raw_json,first_seen_at,last_seen_at)
                VALUES(1,?,?,?,?,'2026-10-01T00:00:00+00:00','2026-10-01T00:00:00+00:00')""",
                (str(number), 'Software Engineer', original['url'], original['raw_json']))
        self.conn.commit()
        client = MagicMock()
        def hosted_call(route, data=None):
            response = self.call(route, data)
            self.assertEqual(response.status_code, 200, response.json)
            return response.json
        client.call.side_effect = hosted_call
        candidate_filter = pipeline.filter_candidate_jobs
        def ordered_candidates(conn, user):
            return sorted(candidate_filter(conn, user), key=lambda job: job['company_id'])
        healthy = dict(external_id='2', title='Software Engineer', url='https://example.test/apply',
                       raw_json=dict(descriptionPlain='Python'))
        with patch.object(pipeline, 'GROQ_API_KEY', 'synthetic'), \
                patch.object(pipeline, 'fetch_jobs_for_company', side_effect=[requests.Timeout(), [healthy]]), \
                patch.object(pipeline, 'filter_candidate_jobs', side_effect=ordered_candidates), \
                patch.object(pipeline, 'score_job_for_user', return_value=test_evaluation.sample_result()) as score, \
                patch.object(runner.time, 'monotonic', return_value=0):
            self.assertEqual(runner.fetch_and_score(client, 2100), 1)
        score.assert_called_once()
        self.assertEqual(score.call_args.args[1]['company_id'], 2)
        self.assertEqual(dict(self.conn.execute('SELECT * FROM jobs WHERE id=1').fetchone()), original)
        self.assertEqual(self.conn.execute('SELECT is_stale FROM scores WHERE job_id=1').fetchone()[0], 1)
        with patch.object(cloud_worker, 'fixed_schedule_due', return_value=True), \
                patch.object(pipeline, 'validate_email_configuration'), \
                patch.object(pipeline, 'deliver_email') as mail:
            self.assertTrue(self.call('email', dict(after=0)).json['sent'])
        self.assertEqual([match['job_id'] for match in mail.call_args.args[1]], [2])

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

    def test_career_description_fingerprint_validation_and_preserved_snapshot(self):
        raw = dict(career_source='hotstar', listing_version='synthetic-version')
        self.conn.execute("UPDATE companies SET career_source='hotstar' WHERE id=1")
        self.conn.execute('UPDATE jobs SET raw_json=? WHERE id=1', (json.dumps(raw),))
        self.conn.commit()
        # Ranking can put a described job ahead of this unresolved career posting.
        item = next(item for item in self.call('work').json['items'] if item['job']['id'] == 1)
        data = dict(user_id=1, job_id=item['job']['id'], fingerprint=item['fingerprint'],
                    description='<p>Python and SQL; complete JD tail.</p>',
                    title=item['job']['title'], location=item['job']['location'])
        self.assertEqual(self.call('description', dict(data, description='')).status_code, 400)
        prepared = self.call('description', data)
        self.assertEqual(prepared.status_code, 200)
        self.assertTrue(prepared.json['eligible'])
        self.assertEqual(self.call('score', self.score_data(item)).status_code, 409)
        updated = dict(item, job=prepared.json['job'], fingerprint=prepared.json['fingerprint'])
        self.assertEqual(self.call('score', self.score_data(updated)).json, {'saved': True})
        snapshot = [dict(external_id=item['job']['external_id'], title=item['job']['title'],
                         location=item['job']['location'], department=item['job']['department'],
                         url=item['job']['url'], raw_json=raw)]
        pipeline.upsert_jobs(self.conn, 1, snapshot)
        stored = self.conn.execute('SELECT raw_json FROM jobs WHERE id=1').fetchone()[0]
        self.assertIn('complete JD tail', stored)
        self.assertEqual(self.conn.execute('SELECT is_stale FROM scores WHERE user_id=1 AND job_id=1').fetchone()[0], 0)
        snapshot[0]['raw_json'] = dict(raw, listing_version='changed-version')
        pipeline.upsert_jobs(self.conn, 1, snapshot)
        self.assertEqual(self.conn.execute('SELECT is_stale FROM scores WHERE user_id=1 AND job_id=1').fetchone()[0], 1)

    def test_career_description_rejects_legacy_job_and_rechecks_location(self):
        item = self.item()
        data = dict(user_id=1, job_id=item['job']['id'], fingerprint=item['fingerprint'],
                    description='<p>Python</p>', title=item['job']['title'], location='Remote')
        self.assertEqual(self.call('description', data).status_code, 400)
        self.conn.execute("UPDATE companies SET career_source='hotstar' WHERE id=1")
        self.conn.execute('UPDATE jobs SET raw_json=? WHERE id=1', (json.dumps(dict(career_source='hotstar')),))
        self.conn.execute('UPDATE users SET preferences_json=? WHERE id=1', (json.dumps(dict(locations=['Delhi'])),))
        self.conn.commit()
        self.conn.execute('UPDATE jobs SET location=NULL WHERE id=1')
        self.conn.commit()
        item = self.item()
        data.update(fingerprint=item['fingerprint'], location='Mumbai')
        response = self.call('description', data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json['eligible'])

    def test_catalog_includes_unavailable_companies_but_rejects_subscription(self):
        self.conn.execute("INSERT INTO companies(name,ats_type,board_slug,career_source,active) VALUES('ICICI Bank','greenhouse','career-site-icici','icici',0)")
        identifier = self.conn.execute("SELECT id FROM companies WHERE career_source='icici'").fetchone()[0]
        self.conn.commit()
        with self.client.session_transaction() as session:
            session['user_id'], session['session_version'] = 1, 0
        response = self.client.get('/settings')
        self.assertIn(b'ICICI Bank', response.data)
        self.assertIn(b'Automatic fetching unavailable', response.data)
        self.assertEqual(test_web_flows.WebFlowTests.post(self, '/settings',
            test_web_flows.WebFlowTests.settings_data(self, companies=[str(identifier)])).status_code, 400)


class CloudRunnerTests(unittest.TestCase):
    def test_gemini_503_retries_are_bounded_and_respect_deadline(self):
        def http_error(status):
            response = requests.Response()
            response.status_code = status
            return requests.HTTPError('private provider details', response=response)

        cases = (
            ('recovered', [http_error(503), http_error(503), {}], 0, [2, 4], 3),
            ('exhausted', [http_error(503)] * 3, 1, [2, 4], 3),
            ('timeout', [requests.ReadTimeout('private timeout')], 1, [], 1),
            ('authentication', [http_error(401)], 1, [], 1),
            ('rate_limit', [http_error(429)], 1, [], 1),
            ('missing_response', [requests.HTTPError('private')], 1, [], 1),
            ('budget_before_wait', [http_error(503)], 1, [], 1),
            ('budget_after_wait', [http_error(503)], 1, [2], 1),
        )
        for name, outcomes, expected, delays, attempts in cases:
            with self.subTest(name=name):
                company = dict(id=1, name='Synthetic', ats_type='lever', board_slug='synthetic', fingerprint='0' * 64)
                item = dict(user=dict(id=1, resume_text='Python'),
                            job=dict(id=1, company_id=1, raw_json='{}'),
                            company_name='Synthetic', fingerprint='0' * 64)
                client = MagicMock()
                client.call.side_effect = [dict(companies=[company]), dict(saved=0),
                    dict(next=1, items=[item]), dict(saved=True), dict(next=None, items=[])]
                clock = [0]
                def evaluate(*args, **kwargs):
                    if kwargs.get('provider') != 'gemini':
                        raise http_error(429)
                    if name == 'budget_before_wait':
                        clock[0] = 2100 - runner.GEMINI_EVALUATION_RESERVE - 1
                    outcome = outcomes.pop(0)
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome
                def sleep(delay):
                    clock[0] = 2100 if name == 'budget_after_wait' else clock[0] + delay
                with patch.object(pipeline, 'GROQ_API_KEY', 'synthetic'), \
                        patch.object(pipeline, 'GEMINI_API_KEY', 'synthetic'), \
                        patch.object(pipeline, 'fetch_jobs_for_company', return_value=[]), \
                        patch.object(pipeline, 'score_job_for_user', side_effect=evaluate) as score, \
                        patch.object(runner.time, 'monotonic', side_effect=lambda: clock[0]), \
                        patch.object(runner.time, 'sleep', side_effect=sleep) as wait, \
                        self.assertLogs(runner.logger, level='INFO') as logs:
                    self.assertEqual(runner.fetch_and_score(client, 2100), expected)
                self.assertEqual(score.call_count, 1 + attempts)
                self.assertEqual([call.args[0] for call in wait.call_args_list], delays)
                saves = [call for call in client.call.call_args_list if call.args[0] == 'score']
                self.assertEqual(len(saves), int(expected == 0))
                self.assertNotIn('private', ' '.join(logs.output))

    def test_scoring_failures_continue_only_for_candidate_rejections(self):
        cases = [(status, status in runner.CANDIDATE_HTTP_STATUSES)
                 for status in (400, 413, 422, 401, 403, 404, 429, 500, 503)]
        cases.extend((error, isinstance(error, (ValueError, KeyError, TypeError))) for error in (
            ValueError('private evaluation'), KeyError('private field'), TypeError('private type'),
            RuntimeError('private configuration'), requests.Timeout('private timeout'),
            requests.ConnectionError('private network'), requests.HTTPError('private missing response')))
        for failure, continues in cases:
            with self.subTest(failure=type(failure).__name__ if isinstance(failure, Exception) else failure):
                error = failure
                if isinstance(failure, int):
                    response = requests.Response()
                    response.status_code = failure
                    response._content = b'private response body'
                    response.headers['Retry-After'] = 'private header'
                    error = requests.HTTPError('private credentials', response=response)
                company = dict(id=1, name='Synthetic', ats_type='lever', board_slug='synthetic', fingerprint='0' * 64)
                items = [dict(user=dict(id=user_id, resume_text='private resume'),
                              job=dict(id=number, company_id=1, raw_json='{}'),
                              company_name='Synthetic', fingerprint='0' * 64)
                         for number, user_id in ((1, 1), (2, 1), (3, 1), (4, 2))]
                saved = []
                client = MagicMock()
                def hosted_call(route, data=None):
                    if route == 'companies':
                        return dict(companies=[company])
                    if route == 'snapshot':
                        return dict(saved=0)
                    if route.startswith('work?after=0&'):
                        return dict(next=1, items=items[:3])
                    if route.startswith('work?after=1&'):
                        return dict(next=2, items=items[3:])
                    if route.startswith('work?after=2&'):
                        return dict(next=None, items=[])
                    if route == 'score':
                        saved.append(data['job_id'])
                        return dict(saved=True)
                    self.fail('Unexpected hosted operation')
                client.call.side_effect = hosted_call
                with patch.object(pipeline, 'GROQ_API_KEY', 'synthetic'), \
                        patch.object(pipeline, 'fetch_jobs_for_company', return_value=[]), \
                        patch.object(pipeline, 'score_job_for_user', side_effect=[{}, error, {}, {}]) as score, \
                        patch.object(runner.time, 'monotonic', return_value=0), \
                        self.assertLogs(runner.logger, level='INFO') as logs:
                    self.assertEqual(runner.fetch_and_score(client, 2100), 1)
                self.assertEqual(score.call_count, 4 if continues else 2)
                self.assertEqual(saved, [1, 3, 4] if continues else [1])
                output = ' '.join(logs.output)
                self.assertIn('user_id=1 job_id=2 stage=evaluation error=' + type(error).__name__, output)
                self.assertIn('http_status=' + (str(failure) if isinstance(failure, int) else 'unavailable'), output)
                self.assertIn('candidate deferred; continuing' if continues else 'remaining work deferred', output)
                if continues:
                    self.assertIn('scores_saved=3 candidates_failed=1 partial_failure=True', output)
                self.assertNotIn('private', output)

    def test_uncertain_score_save_stops_without_retry_or_further_evaluation(self):
        for error in (requests.Timeout('private write'), ValueError('private response'),
                      runner.HostedOperationError(503)):
            with self.subTest(error=type(error).__name__):
                company = dict(id=1, name='Synthetic', ats_type='lever', board_slug='synthetic', fingerprint='0' * 64)
                items = [dict(user=dict(id=1, resume_text='Python'),
                              job=dict(id=number, company_id=1, raw_json='{}'),
                              company_name='Synthetic', fingerprint='0' * 64) for number in (1, 2)]
                client = MagicMock()
                client.call.side_effect = [dict(companies=[company]), dict(saved=0), dict(next=1, items=items), error]
                with patch.object(pipeline, 'GROQ_API_KEY', 'synthetic'), \
                        patch.object(pipeline, 'fetch_jobs_for_company', return_value=[]), \
                        patch.object(pipeline, 'score_job_for_user', return_value={}) as score, \
                        patch.object(runner.time, 'monotonic', return_value=0), \
                        self.assertLogs(runner.logger, level='ERROR') as logs:
                    self.assertEqual(runner.fetch_and_score(client, 2100), 1)
                score.assert_called_once()
                self.assertEqual(client.call.call_count, 4)
                self.assertIn('user_id=1 job_id=1 stage=save', ' '.join(logs.output))
                if isinstance(error, runner.HostedOperationError):
                    self.assertIn('http_status=503', ' '.join(logs.output))
                self.assertNotIn('private', ' '.join(logs.output))

    def test_23_boards_score_only_successful_snapshots_and_preserve_failure_status(self):
        for failed_ids in (set(), {1, 2}, set(range(1, 24))):
            with self.subTest(failed_ids=failed_ids):
                companies = [dict(id=number, name='Synthetic', ats_type='lever',
                                  board_slug='synthetic', fingerprint='0' * 64) for number in range(1, 24)]
                saved, evaluated, requested = [], [], []
                client = MagicMock()
                def hosted_call(route, data=None):
                    if route == 'companies':
                        return dict(companies=companies)
                    if route == 'snapshot':
                        saved.append(data['company_id'])
                        return dict(saved=0)
                    if route.startswith('work?'):
                        from urllib.parse import parse_qs
                        query = parse_qs(route.split('?', 1)[1])
                        allowed = {int(value) for value in query['company_id']}
                        requested.append(allowed)
                        after = int(query['after'][0])
                        if after == 23:
                            return dict(items=[], next=None)
                        number = after + 1
                        items = [dict(user=dict(id=number, resume_text='Python'),
                            job=dict(id=number, company_id=number, raw_json='{}'),
                            company_name='Synthetic', fingerprint='0' * 64)] if number in allowed else []
                        return dict(items=items, next=number)
                    if route == 'score':
                        evaluated.append(data['job_id'])
                        return dict(saved=True)
                    self.fail('Unexpected hosted operation')
                client.call.side_effect = hosted_call
                def fetch(company):
                    if company['id'] in failed_ids:
                        raise requests.Timeout()
                    return []
                with patch.object(pipeline, 'GROQ_API_KEY', 'synthetic'), \
                        patch.object(pipeline, 'fetch_jobs_for_company', side_effect=fetch), \
                        patch.object(pipeline, 'score_job_for_user', return_value={}) as score, \
                        patch.object(runner.time, 'monotonic', return_value=0):
                    if len(failed_ids) == 23:
                        with self.assertRaises(RuntimeError):
                            runner.fetch_and_score(client, 2100)
                        score.assert_not_called()
                        self.assertEqual(requested, [])
                    else:
                        self.assertEqual(runner.fetch_and_score(client, 2100), int(bool(failed_ids)))
                        self.assertEqual(set(evaluated), set(range(1, 24)) - failed_ids)
                        self.assertTrue(all(allowed == set(saved) for allowed in requested))
                self.assertEqual(set(saved), set(range(1, 24)) - failed_ids)

    def test_evaluation_budget_defers_candidates_without_saving_them(self):
        client = MagicMock()
        saved = []
        def hosted_call(route, data=None):
            if route == 'companies':
                return dict(companies=[dict(id=1, name='Synthetic', ats_type='lever',
                    board_slug='synthetic', fingerprint='0' * 64)])
            if route == 'snapshot':
                return dict(saved=0)
            if route.startswith('work?'):
                after = int(route.split('after=')[1].split('&')[0])
                user_id = after + 1
                return dict(next=user_id, items=[dict(user=dict(id=user_id, resume_text='Python'),
                    job=dict(id=after * 5 + number, company_id=1, raw_json='{}'),
                    company_name='Synthetic', fingerprint='0' * 64) for number in range(1, 6)])
            if route == 'score':
                saved.append(data['job_id'])
                return dict(saved=True)
            self.fail('Unexpected hosted operation')
        client.call.side_effect = hosted_call
        with patch.object(pipeline, 'GROQ_API_KEY', 'synthetic'), \
                patch.object(pipeline, 'fetch_jobs_for_company', return_value=[]), \
                patch.object(pipeline, 'score_job_for_user', return_value={}) as score, \
                patch.object(runner.time, 'monotonic', return_value=0):
            self.assertEqual(runner.fetch_and_score(client, 2100), 1)
        self.assertEqual(score.call_count, runner.MAX_SCORES)
        self.assertEqual(saved, list(range(1, 101)))

    def test_description_response_identity_is_checked_before_groq(self):
        for changed in (False, True):
            with self.subTest(changed=changed):
                client = MagicMock()
                company = dict(id=1, name='Synthetic', ats_type='greenhouse', board_slug='career-site-hotstar',
                               career_source='hotstar', fingerprint='0' * 64)
                job = dict(id=1, company_id=1, title='Analyst', location='India', raw_json='{}')
                prepared = dict(job, raw_json=json.dumps(dict(career_source='hotstar', descriptionHtml='Python')))
                item = dict(user=dict(id=1, resume_text='Python'), job=job, company_name='Synthetic', fingerprint='0' * 64)
                reply = dict(eligible=True, job=dict(prepared, id=99 if changed else 1), fingerprint='1' * 64)
                client.call.side_effect = [dict(companies=[company]), dict(saved=0), dict(items=[item], next=1),
                                          reply, dict(saved=True), dict(items=[], next=None)]
                with patch.object(pipeline, 'GROQ_API_KEY', 'synthetic'), \
                        patch.object(runner.time, 'monotonic', return_value=0), \
                        patch.object(runner.career_sources, 'fetch', return_value=[]), \
                        patch.object(runner.career_sources, 'prepare_job', return_value=prepared), \
                        patch.object(pipeline, 'score_job_for_user', return_value={}) as score:
                    self.assertEqual(runner.fetch_and_score(client, 2100), int(changed))
                if changed:
                    score.assert_not_called()
                else:
                    score.assert_called_once()
                    self.assertEqual(client.call.call_args_list[-2].args[1]['fingerprint'], '1' * 64)

    def test_description_failure_does_not_prevent_other_candidates_from_scoring(self):
        client = MagicMock()
        company = dict(id=1, name='Synthetic', ats_type='lever', board_slug='synthetic', fingerprint='0' * 64)
        items = [dict(user=dict(id=1, resume_text='Python'),
                      job=dict(id=number, company_id=1, raw_json='{}'), company_name='Synthetic', fingerprint='0' * 64)
                 for number in (1, 2)]
        client.call.side_effect = [dict(companies=[company]), dict(saved=0),
                                  dict(items=items, next=1), dict(saved=True), dict(items=[], next=None)]
        with patch.object(pipeline, 'GROQ_API_KEY', 'synthetic'), \
                patch.object(pipeline, 'fetch_jobs_for_company', return_value=[]), \
                patch.object(runner.time, 'monotonic', return_value=0), \
                patch.object(runner.career_sources, 'prepare_job', side_effect=[requests.ReadTimeout(), items[1]['job']]), \
                patch.object(pipeline, 'score_job_for_user', return_value={}) as score:
            self.assertEqual(runner.fetch_and_score(client, 2100), 1)
        score.assert_called_once()
        self.assertEqual(score.call_args.args[1]['id'], 2)

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

    def test_observed_worker_chunks_after_deduplication_and_reports_failed_transfer(self):
        company = dict(id=1, name='Synthetic', ats_type='lever', board_slug='synthetic',
                       career_source='wipro', fingerprint='0' * 64)
        jobs = [dict(external_id=str(number), title='Engineer', raw_json={}) for number in range(3)]
        for fail_last in (False, True):
            client = MagicMock()
            client.call.side_effect = ([dict(companies=[company]), dict(saved=2), requests.Timeout()]
                                       if fail_last else [dict(companies=[company]), dict(saved=2), dict(saved=1)])
            with patch.object(runner.career_sources, 'fetch', return_value=jobs + [jobs[0]]), \
                    patch.object(runner, 'MAX_JOBS', 2), \
                    patch.object(runner.time, 'monotonic', return_value=0):
                if fail_last:
                    with self.assertLogs(runner.logger, level='ERROR'), self.assertRaises(RuntimeError):
                        runner.fetch_and_score(client, 420, fetch_only=True)
                else:
                    self.assertEqual(runner.fetch_and_score(client, 420, fetch_only=True), 0)
            batches = [call.args[1] for call in client.call.call_args_list if call.args[0] == 'snapshot']
            self.assertEqual([len(batch['jobs']) for batch in batches], [2, 1])
            self.assertTrue(all(batch['mode'] == 'observed' for batch in batches))

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
            with self.assertRaises(runner.HostedOperationError) as error:
                client.call("companies")
            self.assertEqual(error.exception.status, 302)
            response.status_code = 200
            response.iter_content.return_value = [b"x" * (cloud_worker.MAX_BODY_BYTES + 1)]
            with self.assertRaises(ValueError):
                client.call("companies")
            client.close()
            session.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
