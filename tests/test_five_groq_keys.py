"""Five-key rotation, cooldown and history compatibility without live services."""

import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
from flask import render_template

from backend import groq_limits, workflow_monitor
from backend.migrate import apply_migrations
from scripts import run_cloud_worker as runner
from tests import test_cloud_worker, test_web_flows


class FiveKeyPoolTests(unittest.TestCase):
    def setUp(self):
        self.clock = 100
        self.now = datetime(2026, 11, 9, tzinfo=timezone.utc)
        for context in (
            patch.object(runner.pipeline, 'GROQ_API_KEY', 'synthetic-one'),
            patch.dict(runner.os.environ, GROQ_API_KEY_2='synthetic-two',
                       GROQ_API_KEY_3='synthetic-three', GROQ_API_KEY_4='synthetic-four',
                       GROQ_API_KEY_5='synthetic-five'),
            patch.object(runner.time, 'monotonic', side_effect=lambda: self.clock),
            patch.object(runner.GroqKeyPool, 'publish'),
            patch.object(groq_limits, 'datetime', wraps=datetime),
        ):
            mocked = context.start()
            self.addCleanup(context.stop)
        mocked.now.return_value = self.now
        self.client = MagicMock()
        self.client.call.return_value = {'saved': True}

    def limited(self, seconds):
        response = requests.Response()
        response.status_code = 429
        response.headers['retry-after'] = str(seconds)
        response._content = b'{}'
        return requests.HTTPError(response=response)

    def test_first_three_limits_use_four_then_five_and_keep_using_five(self):
        pool = runner.GroqKeyPool(self.client, 3000)
        provider = MagicMock(side_effect=[self.limited(120)] * 3 + ['four', self.limited(120), 'five', 'five'])
        self.assertEqual(pool._request(provider), 'four')
        self.assertEqual(pool._request(provider), 'five')
        self.assertEqual(pool._request(provider), 'five')
        self.assertEqual([call.args[0] for call in provider.call_args_list],
                         [None, 'synthetic-two', 'synthetic-three', 'synthetic-four',
                          'synthetic-four', 'synthetic-five', 'synthetic-five'])

    def test_all_five_cool_down_then_earliest_ready_key_resumes(self):
        quota = groq_limits.QuotaState()
        pool = runner.GroqKeyPool(self.client, 3000, quota=quota)
        provider = MagicMock(side_effect=[self.limited(seconds) for seconds in (120, 100, 90, 60, 80)])
        with self.assertRaises(groq_limits.CooldownPending) as pending:
            pool._request(provider)
        self.assertEqual(pending.exception.seconds, 60)
        self.assertEqual(provider.call_count, 5)
        next_batch = runner.GroqKeyPool(self.client, 3000, quota=quota)
        resumed = MagicMock(return_value='done')
        with self.assertRaises(groq_limits.CooldownPending):
            next_batch._request(resumed)
        resumed.assert_not_called()
        self.clock += 60
        self.assertEqual(next_batch._request(resumed), 'done')
        resumed.assert_called_once_with('synthetic-four')

    def test_blank_fourth_is_skipped_and_duplicate_fifth_is_rejected(self):
        with patch.dict(runner.os.environ, GROQ_API_KEY_4=''):
            pool = runner.GroqKeyPool(self.client, 3000)
            provider = MagicMock(side_effect=[self.limited(120)] * 3 + ['done'])
            self.assertEqual(pool._request(provider), 'done')
            self.assertEqual(provider.call_args.args, ('synthetic-five',))
        with patch.dict(runner.os.environ, GROQ_API_KEY_5='synthetic-one'):
            with self.assertRaisesRegex(ValueError, 'distinct'):
                runner.GroqKeyPool(self.client, 3000)

    def test_new_quota_slots_preserve_bounds(self):
        quota = groq_limits.QuotaState()
        for slot in (3, 4):
            quota.record(slot, {'retry_at': (self.now + timedelta(seconds=60)).isoformat()})
            self.assertEqual(quota.delay(slot), 60)
        for slot in (-1, 5, True):
            with self.assertRaises(ValueError):
                quota.record(slot, {'remaining_tokens': 0})


class FiveKeyEndpointTests(unittest.TestCase):
    setUp = test_cloud_worker.CloudEndpointTests.setUp
    call = test_cloud_worker.CloudEndpointTests.call

    def test_dashboard_history_keeps_all_five_slots_for_twenty_runs(self):
        self.conn.executemany("""INSERT INTO groq_key_history
            (run_id,key_slot,state,outcome,revision,started_at,updated_at,job_count)
            VALUES(?,?,'succeeded','complete',2,'2026-11-09T00:00:00+00:00',
                   '2026-11-09T00:00:00+00:00',1)""",
            [(format(run, '032x'), slot) for run in range(21) for slot in range(1, 6)])
        self.conn.execute('UPDATE accounts SET is_admin=1 WHERE user_id=1')
        self.conn.commit()
        with self.client.session_transaction() as session:
            session['user_id'], session['session_version'] = 1, 0
        with patch('backend.app.render_template', wraps=render_template) as render:
            self.assertEqual(self.client.get('/').status_code, 200)
        history = render.call_args.kwargs['groq_history']
        self.assertEqual(len(history), 20)
        self.assertTrue(all([key['key_slot'] for key in run['keys']] == [1, 2, 3, 4, 5]
                            for run in history))

    def test_five_statuses_and_limits_reach_monitor_and_validate_lengths(self):
        data = dict(run_id='e' * 32, revision=0, states=['not_attempted'] * 5,
                    job_counts=[0] * 5, outcome='running')
        self.assertEqual(self.call('groq-status', data).status_code, 200)
        data.update(revision=1, states=['not_attempted'] * 4 + ['attempting'])
        self.assertEqual(self.call('groq-status', data).status_code, 200)
        data.update(revision=2, states=['not_attempted'] * 4 + ['succeeded'], job_counts=[0] * 4 + [1])
        self.assertEqual(self.call('groq-status', data).status_code, 200)
        for slot in (4, 5):
            payload = dict(key_slot=slot, model='fixture', metrics={'remaining_tokens': 42})
            self.assertEqual(self.call('groq-limits', payload).status_code, 200)
        keys = workflow_monitor.snapshot(self.conn, 1)['groq_keys']
        self.assertEqual([key['key_slot'] for key in keys], [1, 2, 3, 4, 5])
        self.assertEqual(keys[4]['job_count'], 1)
        self.assertEqual(keys[4]['limits']['remaining_tokens'], 42)
        for length in (0, 1, 4, 6):
            bad = dict(data, run_id='f' * 32, revision=0, states=['not_attempted'] * length,
                       job_counts=[0] * length)
            self.assertEqual(self.call('groq-status', bad).status_code, 400)
        self.assertEqual(self.call('groq-status', dict(data, job_counts=[0] * 3)).status_code, 400)
        self.assertEqual(self.call('groq-status', dict(data, states=data['states'][:3],
                                                    job_counts=[0] * 3)).status_code, 400)
        self.assertEqual(self.call('groq-limits', dict(payload, key_slot=6)).status_code, 400)


class FiveKeyMigrationTests(unittest.TestCase):
    def test_migration_retains_history_limits_and_ids_and_is_idempotent(self):
        migrations = sorted((Path(__file__).parents[1] / 'backend/migrations').glob('*.sql'))
        with patch('backend.migrate.Path.glob', return_value=[path for path in migrations
                                                           if path.name != '022_five_groq_keys.sql']):
            test_web_flows.WebFlowTests.setUp(self)
        self.conn.execute("""INSERT INTO groq_key_history
            (id,run_id,key_slot,state,outcome,revision,started_at,updated_at,job_count)
            VALUES(42,?,3,'succeeded','complete',2,'2026-11-09','2026-11-09',7)""", ('a' * 32,))
        self.conn.execute("INSERT INTO groq_limits VALUES(3,'fixture',?,'2026-11-09')",
                          (json.dumps({'remaining_tokens': 42}),))
        self.conn.commit()
        history = [tuple(row) for row in self.conn.execute('SELECT * FROM groq_key_history')]
        limits = [tuple(row) for row in self.conn.execute('SELECT * FROM groq_limits')]
        for _ in range(2):
            apply_migrations(self.conn)
            self.assertEqual([tuple(row) for row in self.conn.execute('SELECT * FROM groq_key_history')], history)
            self.assertEqual([tuple(row) for row in self.conn.execute('SELECT * FROM groq_limits')], limits)
        self.conn.execute("INSERT INTO groq_limits VALUES(5,'fixture','{}','2026-11-09')")
        self.conn.execute("""INSERT INTO groq_key_history
            (run_id,key_slot,state,outcome,revision,started_at,updated_at,job_count)
            VALUES(?,5,'succeeded','complete',2,'2026-11-09','2026-11-09',1)""", ('a' * 32,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO groq_limits VALUES(6,'fixture','{}','2026-11-09')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE groq_key_history SET key_slot=6 WHERE key_slot=5")
