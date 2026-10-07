import unittest
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import requests

from backend import matching_tasks, migrate
from scripts import run_pipeline as runner
from tests import test_pipeline, test_web_flows

NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)


class MatchingTaskTests(unittest.TestCase):
    setUp = test_pipeline.PipelineTests.setUp
    prepare = test_pipeline.PipelineTests.prepare
    fetched = test_pipeline.PipelineTests.fetched
    result = test_pipeline.PipelineTests.result
    post = test_web_flows.WebFlowTests.post
    settings_data = test_web_flows.WebFlowTests.settings_data

    def enqueue(self, reason='companies'):
        with self.conn:
            matching_tasks.enqueue(self.conn, 1, reason, NOW)

    def task(self):
        return self.conn.execute('SELECT * FROM matching_tasks WHERE user_id=1').fetchone()

    def test_company_and_preference_changes_coalesce_but_email_changes_do_not(self):
        self.prepare()
        initial = self.settings_data(companies=['1'])
        self.assertEqual(self.post('/settings', initial).status_code, 302)
        first = self.task()['revision']
        self.assertEqual(self.post('/settings', initial).status_code, 302)
        self.assertEqual(self.task()['revision'], first)
        changed = dict(initial, cadence='weekly', min_score='90', send_time='10:00')
        self.assertEqual(self.post('/settings', changed).status_code, 302)
        self.assertEqual(self.task()['revision'], first)
        self.assertEqual(self.post('/settings', dict(changed, companies=['1', '2'])).status_code, 302)
        self.assertEqual(self.task()['revision'], first + 1)
        self.assertEqual(self.task()['reason'], 'companies')
        self.assertEqual(self.post('/settings', dict(changed, roles='Data Engineer')).status_code, 302)
        self.assertEqual(self.task()['reason'], 'preferences')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM matching_tasks').fetchone()[0], 1)

    def test_resume_change_queues_once_and_unchanged_resume_does_not(self):
        self.prepare()
        form = dict(resume_text='Updated Python resume', consent='on')
        self.assertEqual(self.post('/resume', form).status_code, 302)
        self.assertEqual(self.task()['reason'], 'resume')
        revision = self.task()['revision']
        self.assertEqual(self.post('/resume', form).status_code, 302)
        self.assertEqual(self.task()['revision'], revision)
        self.assertEqual(self.post('/resume', dict(form, resume_text='Another resume', consent='')).status_code, 400)
        self.assertEqual(self.task()['revision'], revision)

    def test_queue_failure_rolls_back_profile_changes(self):
        self.prepare()
        before = dict(self.conn.execute('SELECT * FROM users WHERE id=1').fetchone())
        with patch.object(matching_tasks, 'enqueue', side_effect=ValueError('Synthetic queue failure')):
            self.assertEqual(self.post('/settings', self.settings_data()).status_code, 400)
        self.assertEqual(dict(self.conn.execute('SELECT * FROM users WHERE id=1').fetchone()), before)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM company_subscriptions').fetchone()[0], 0)

    def test_new_edit_is_not_cleared_by_completion_of_old_revision(self):
        self.prepare()
        self.enqueue()
        old = matching_tasks.claim(self.conn)[0]
        self.assertEqual(self.task()['status'], 'running')
        self.enqueue('resume')
        matching_tasks.finish(self.conn, old, 'complete', NOW)
        self.assertEqual(self.task()['status'], 'pending')
        self.assertEqual(self.task()['revision'], old['revision'] + 1)
        latest = matching_tasks.claim(self.conn)[0]
        matching_tasks.finish(self.conn, latest, 'complete', NOW)
        self.assertEqual(self.task()['status'], 'complete')

    def test_abandoned_running_task_is_reclaimed_and_failed_task_retried(self):
        self.prepare()
        self.enqueue()
        first = matching_tasks.claim(self.conn)[0]
        recovered = matching_tasks.claim(self.conn)[0]
        self.assertEqual(first['revision'], recovered['revision'])
        matching_tasks.finish(self.conn, recovered, 'failed', NOW, 'scoring_failed')
        self.assertEqual(matching_tasks.claim(self.conn)[0]['revision'], first['revision'])

    def test_matching_only_refreshes_selected_company_and_never_sends_email(self):
        self.prepare()
        self.post('/settings', self.settings_data(companies=['1'], roles='', locations=''))
        with patch.object(runner.pipeline, 'fetch_jobs_for_company', side_effect=self.fetched) as fetch, \
                patch.object(runner.pipeline, 'score_job_for_user', side_effect=self.result) as score:
            self.assertEqual(runner.main(['--run', '--matching-only']), 0)
        self.assertEqual([call.args[0]['id'] for call in fetch.call_args_list], [1])
        self.assertEqual(score.call_count, 1)
        self.mail.assert_not_called()
        self.assertEqual(self.task()['status'], 'complete')

    def test_budget_keeps_task_pending_until_all_openings_checked(self):
        self.prepare()
        self.enqueue()
        with patch.object(runner.pipeline, 'fetch_jobs_for_company', side_effect=self.fetched), \
                patch.object(runner.pipeline, 'score_job_for_user', side_effect=self.result):
            self.assertEqual(runner.main(['--run', '--matching-only', '--max-scores', '1']), 0)
            self.assertEqual(self.task()['status'], 'pending')
            self.assertEqual(runner.main(['--run', '--matching-only', '--max-scores', '1']), 0)
        self.assertEqual(self.task()['status'], 'complete')

    def test_no_companies_selected_completes_without_fetching_or_scoring(self):
        self.prepare()
        self.post('/settings', self.settings_data(companies=[]))
        with patch.object(runner.pipeline, 'fetch_jobs_for_company') as fetch, \
                patch.object(runner.pipeline, 'score_job_for_user') as score:
            self.assertEqual(runner.main(['--run', '--matching-only']), 0)
        fetch.assert_not_called()
        score.assert_not_called()
        self.assertEqual(self.task()['status'], 'complete')

    def test_failed_refresh_reports_failure_and_keeps_old_jobs(self):
        self.prepare()
        self.enqueue()
        with patch.object(runner.pipeline, 'fetch_jobs_for_company', side_effect=requests.Timeout), \
                self.assertLogs(runner.logger, level='ERROR'):
            self.assertEqual(runner.main(['--run', '--matching-only']), 1)
        self.assertEqual(self.task()['status'], 'failed')
        self.assertEqual(self.task()['last_error'], 'fetch_failed')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM jobs WHERE is_active=1').fetchone()[0], 2)

    def test_missing_resume_waits_without_external_calls(self):
        self.prepare()
        self.conn.execute("UPDATE users SET resume_text=''")
        self.conn.commit()
        self.enqueue()
        with patch.object(runner.pipeline, 'fetch_jobs_for_company') as fetch:
            self.assertEqual(runner.main(['--run', '--matching-only']), 0)
        fetch.assert_not_called()
        self.assertEqual(matching_tasks.ready(self.conn), [])
        self.assertIn(b'Add a resume to start matching', self.client.get('/').data)

    def test_task_status_is_specific_to_signed_in_user(self):
        self.prepare()
        self.conn.execute("INSERT INTO users(id,name,email,resume_text) VALUES(2,'Other','other@example.test','Other resume')")
        self.conn.commit()
        with self.conn:
            matching_tasks.enqueue(self.conn, 2, 'resume', NOW)
        self.assertNotIn(b'Matching pending.', self.client.get('/').data)
        self.enqueue()
        self.assertIn(b'Matching pending.', self.client.get('/').data)

    def test_idle_matching_worker_and_preview_are_read_only(self):
        self.prepare()
        before = self.database.read_bytes()
        with patch.object(runner.pipeline, 'fetch_jobs_for_company') as fetch:
            self.assertEqual(runner.main(['--matching-only']), 0)
            self.assertEqual(runner.main(['--run', '--matching-only']), 0)
        fetch.assert_not_called()
        self.assertEqual(before, self.database.read_bytes())

    def test_matching_runs_even_when_email_delivery_is_paused(self):
        self.prepare()
        self.enqueue()
        marker = self.database.parent / 'email_schedule.paused'
        marker.write_text('Synthetic pause', encoding='utf-8')
        with patch.object(runner.pipeline, 'fetch_jobs_for_company', side_effect=self.fetched), \
                patch.object(runner.pipeline, 'score_job_for_user', side_effect=self.result):
            self.assertEqual(runner.main(['--run', '--matching-only']), 0)
        self.assertTrue(marker.exists())
        self.mail.assert_not_called()

    def test_migration_backfills_only_eligible_accounts_without_changing_profiles(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.executescript(runner.pipeline.SCHEMA)
        files = sorted((Path(migrate.__file__).parent / 'migrations').glob('*.sql'))
        with patch.object(Path, 'glob', return_value=files[:3]):
            migrate.apply_migrations(conn)
        for number in (1, 2, 3):
            conn.execute("INSERT INTO users(id,name,email,resume_text,active) VALUES(?,?,?,?,?)",
                         (number, 'Synthetic profile', f'user{number}@example.test', 'Original resume', int(number != 2)))
            conn.execute("INSERT INTO accounts(user_id,password_hash,email_verified,created_at) VALUES(?,?,?,?)",
                         (number, 'unused-fixture', int(number != 3), NOW.isoformat()))
        conn.commit()
        before = conn.execute('SELECT * FROM users ORDER BY id').fetchall()
        migrate.apply_migrations(conn)
        self.assertEqual(conn.execute('SELECT * FROM users ORDER BY id').fetchall(), before)
        self.assertEqual(conn.execute('SELECT user_id,revision,status FROM matching_tasks').fetchall(), [(1, 1, 'pending')])
        migrate.apply_migrations(conn)
        self.assertEqual(conn.execute('SELECT revision FROM matching_tasks').fetchone()[0], 1)

    def test_posting_changes_queue_only_following_users_and_unchanged_fetch_does_not(self):
        self.prepare()
        self.post('/settings', self.settings_data(companies=['1']))
        self.conn.execute("UPDATE matching_tasks SET status='complete'")
        self.conn.execute("INSERT INTO users(id,name,email,resume_text) VALUES(2,'Other','other@example.test','Python resume')")
        self.conn.execute("INSERT INTO accounts(user_id,password_hash,created_at) VALUES(2,'synthetic','2026-10-08')")
        self.conn.execute("INSERT INTO user_settings(user_id,subscriptions_configured) VALUES(2,1)")
        self.conn.execute("INSERT INTO company_subscriptions(user_id,company_id,subscribed_at) VALUES(2,2,?)", (NOW.isoformat(),))
        self.conn.commit()
        with patch.object(runner.pipeline, 'fetch_jobs_for_company', side_effect=self.fetched):
            runner.fetch_cycle(self.conn, {1})
            revision = self.task()['revision']
            self.assertEqual(self.task()['status'], 'pending')
            self.assertEqual(matching_tasks.status(self.conn, 2), None)
            runner.fetch_cycle(self.conn, {1})
        self.assertEqual(self.task()['revision'], revision)

    def test_task_validation_rejects_invalid_ids_reasons_and_naive_time(self):
        self.prepare()
        for user_id, reason, now in ((0, 'resume', NOW), (1, 'unknown', NOW), (1, 'resume', NOW.replace(tzinfo=None))):
            with self.assertRaises(ValueError):
                matching_tasks.enqueue(self.conn, user_id, reason, now)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM matching_tasks').fetchone()[0], 0)
