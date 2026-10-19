import copy
import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
from flask import render_template

from backend import cloud_worker, pipeline_skeleton as pipeline
from backend.migrate import apply_migrations
from scripts import run_cloud_worker as runner
from tests import test_cloud_worker, test_web_flows


class PreHistoryMigrationTests(unittest.TestCase):
    def setUp(self):
        migrations = sorted((Path(__file__).parents[1] / 'backend/migrations').glob('*.sql'))
        previous = [path for path in migrations if path.name != '008_groq_key_history.sql']
        # Reproduce a deployed database that predates the new history feature.
        with patch('backend.migrate.Path.glob', return_value=previous):
            test_web_flows.WebFlowTests.setUp(self)
        test_web_flows.WebFlowTests.seed_user(self)

    def test_admin_dashboard_keeps_analytics_when_history_migration_is_missing(self):
        self.conn.execute('UPDATE accounts SET is_admin=1 WHERE user_id=1')
        self.conn.commit()
        with self.assertLogs(self.app.logger, level='ERROR') as logs:
            page = self.client.get('/')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'Match score trend', page.data)
        self.assertIn(b'Software Engineer 1', page.data)
        self.assertNotIn(b'Groq key history', page.data)
        self.assertNotIn(b'Saved dashboard data could not be loaded', page.data)
        self.assertNotIn(b'No matching run history recorded yet.', page.data)
        self.assertIn('008_groq_key_history.sql', ' '.join(logs.output))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM scores').fetchone()[0], 2)
        apply_migrations(self.conn)
        page = self.client.get('/')
        self.assertEqual(page.status_code, 200)
        self.assertNotIn(b'history is unavailable', page.data)
        self.assertNotIn(b'No matching run history recorded yet.', page.data)

    def test_nonadmin_dashboard_does_not_need_history_migration(self):
        page = self.client.get('/')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'Match score trend', page.data)
        self.assertNotIn(b'Groq key history', page.data)


class GroqHistoryTests(unittest.TestCase):
    setUp = test_cloud_worker.CloudEndpointTests.setUp
    call = test_cloud_worker.CloudEndpointTests.call

    def initial(self, run_id='a' * 32):
        return dict(run_id=run_id, revision=0, states=['not_attempted'] * 3, outcome='running')

    def record(self, payload):
        response = self.call('groq-status', payload)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json, dict(saved=True, revision=payload['revision']))

    def test_status_endpoint_requires_worker_auth_and_rejects_browser_session(self):
        self.assertEqual(self.client.post('/internal/worker/groq-status', json=self.initial()).status_code, 401)
        with self.client.session_transaction() as session:
            session['user_id'], session['session_version'] = 1, 0
        self.assertEqual(self.call('groq-status', self.initial()).status_code, 401)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM groq_key_history').fetchone()[0], 0)

    def test_status_validation_is_bounded_and_atomic(self):
        for mutation in (dict(run_id='../private'), dict(run_id='a' * 33), dict(revision=True),
                         dict(revision=1001), dict(states=['not_attempted']), dict(states=[{}, '', None]),
                         dict(outcome='private failure'), dict(states=['succeeded'] * 3),
                         dict(api_key='synthetic-not-to-store')):
            with self.subTest(fields=list(mutation)):
                self.assertEqual(self.call('groq-status', dict(self.initial(), **mutation)).status_code, 400)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM groq_key_history').fetchone()[0], 0)
        data = self.initial()
        self.record(data)
        invalid = dict(data, revision=1, states=['attempting', 'rate_limited', 'not_attempted'])
        self.assertEqual(self.call('groq-status', invalid).status_code, 400)
        rows = self.conn.execute('SELECT state,revision FROM groq_key_history').fetchall()
        self.assertTrue(all(row['state'] == 'not_attempted' and row['revision'] == 0 for row in rows))

    def test_idempotency_stale_revisions_and_terminal_limits(self):
        data = self.initial()
        self.record(data)
        before = [dict(row) for row in self.conn.execute('SELECT * FROM groq_key_history')]
        self.record(data)
        self.assertEqual(before, [dict(row) for row in self.conn.execute('SELECT * FROM groq_key_history')])
        self.assertEqual(self.call('groq-status', dict(data, revision=2)).status_code, 409)
        data.update(revision=1, states=['attempting', 'not_attempted', 'not_attempted'])
        self.record(data)
        self.assertEqual(self.call('groq-status', self.initial()).status_code, 409)
        data.update(revision=2, states=['rate_limited', 'not_attempted', 'not_attempted'])
        self.record(data)
        self.assertEqual(self.call('groq-status', dict(data, revision=3, states=['succeeded'] * 3)).status_code, 400)
        data.update(revision=3, outcome='deferred')
        self.record(data)
        self.record(data)
        self.assertEqual(self.call('groq-status', dict(data, revision=4)).status_code, 409)
        apply_migrations(self.conn)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM groq_key_history').fetchone()[0], 3)

    def test_worker_lock_contention_does_not_write_status(self):
        with cloud_worker.run_pipeline.exclusive_cycle(self.database.parent / 'pipeline.lock'):
            self.assertEqual(self.call('groq-status', self.initial()).status_code, 503)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM groq_key_history').fetchone()[0], 0)

    def test_dashboard_omits_history_for_all_accounts_and_preserves_runs(self):
        data = self.initial()
        self.record(data)
        data.update(revision=1, states=['attempting', 'not_attempted', 'not_attempted'])
        self.record(data)
        data.update(revision=2, states=['rate_limited', 'not_attempted', 'not_attempted'])
        self.record(data)
        data.update(revision=3, outcome='deferred')
        self.record(data)
        self.record(self.initial('b' * 32))
        with self.client.session_transaction() as session:
            session['user_id'], session['session_version'] = 1, 0
        self.assertNotIn(b'Groq key history', self.client.get('/').data)
        self.assertNotIn(b'Rate limit hit', self.client.get('/?history_before=1').data)
        self.conn.execute('UPDATE accounts SET is_admin=1 WHERE user_id=1')
        self.conn.commit()
        page = self.client.get('/')
        self.assertNotIn(b'Groq key history', page.data)
        self.assertNotIn(b'id="groq-history"', page.data)
        self.assertNotIn(b'Rate limit hit', page.data)
        self.assertNotIn(b'Key 2:</strong> Not attempted', page.data)
        self.assertNotIn(b'a' * 32, page.data)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM groq_key_history').fetchone()[0], 6)
        self.assertNotIn(b'Groq key history', self.client.get('/jobs').data)
        self.assertEqual(self.client.get('/?history_before=-1').status_code, 400)

    def test_history_pagination_is_bounded_and_has_no_overlap(self):
        for number in range(22):
            self.record(self.initial(format(number, '032x')))
        self.conn.execute('UPDATE accounts SET is_admin=1 WHERE user_id=1')
        self.conn.commit()
        with self.client.session_transaction() as session:
            session['user_id'], session['session_version'] = 1, 0
        with patch('backend.app.render_template', wraps=render_template) as render:
            page = self.client.get('/')
        self.assertEqual(len(render.call_args.kwargs['groq_history']), 20)
        cursor = str(render.call_args.kwargs['groq_history_next'])
        self.assertNotIn(b'id="groq-history"', page.data)
        with patch('backend.app.render_template', wraps=render_template) as render:
            older = self.client.get('/?history_before=' + cursor)
        self.assertEqual(len(render.call_args.kwargs['groq_history']), 2)
        self.assertNotIn(b'>Older runs</a>', older.data)
        with self.app.test_request_context('/?history_before=' + cursor):
            # Run identities are not exposed in HTML; cursor pagination is based on DB IDs.
            recent = self.conn.execute('SELECT id FROM groq_key_history WHERE key_slot=1 AND id>=?', (int(cursor),)).fetchall()
            old = self.conn.execute('SELECT id FROM groq_key_history WHERE key_slot=1 AND id<?', (int(cursor),)).fetchall()
        self.assertEqual(len(recent), 20)
        self.assertEqual(len(old), 2)


class GroqReportingFailureTests(unittest.TestCase):
    def test_uncertain_status_write_stops_without_retry_or_provider_request(self):
        client = MagicMock()
        client.call.side_effect = requests.ReadTimeout('private details')
        with patch.object(pipeline, 'GROQ_API_KEY', 'synthetic'), \
                patch.dict(pipeline.os.environ, GROQ_API_KEY_2='', GROQ_API_KEY_3=''), \
                patch.object(pipeline, 'score_job_for_user') as score:
            with self.assertRaisesRegex(RuntimeError, 'Could not record'):
                runner.fetch_and_score(client, 2100)
        client.call.assert_called_once()
        score.assert_not_called()

    def test_failed_status_after_rate_limit_does_not_advance_or_retry_in_cleanup(self):
        client = MagicMock()
        reports = []
        def report(route, data=None):
            reports.append(copy.deepcopy(data))
            if len(reports) == 3:
                raise requests.ReadTimeout('private write')
            return dict(saved=True, revision=data['revision'])
        client.call.side_effect = report
        response = requests.Response()
        response.status_code = 429
        with patch.object(pipeline, 'GROQ_API_KEY', 'synthetic-1'), \
                patch.dict(pipeline.os.environ, GROQ_API_KEY_2='synthetic-2', GROQ_API_KEY_3='synthetic-3'), \
                patch.object(runner.time, 'monotonic', return_value=0), \
                patch.object(pipeline, 'score_job_for_user', side_effect=requests.HTTPError(response=response)) as score:
            pool = runner.GroqKeyPool(client, 2100)
            with self.assertRaises(RuntimeError):
                pool.score({}, {}, 'Synthetic')
        self.assertTrue(pool.reporting_failed)
        self.assertEqual(client.call.call_count, 3)
        score.assert_called_once()
        self.assertNotIn('synthetic', json.dumps(reports))
