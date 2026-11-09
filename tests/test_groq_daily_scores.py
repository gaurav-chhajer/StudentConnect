"""Daily evaluation totals use acknowledged deltas, not a batch's start date."""

import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from backend import cloud_worker, workflow_monitor as monitor
from backend.migrate import apply_migrations
from tests import test_cloud_worker, test_web_flows


BEFORE_MIDNIGHT = datetime(2026, 11, 9, 18, 29, 59, tzinfo=timezone.utc)
MIDNIGHT = datetime(2026, 11, 9, 18, 30, tzinfo=timezone.utc)


class DailyScoreTests(unittest.TestCase):
    setUp = test_cloud_worker.CloudEndpointTests.setUp
    call = test_cloud_worker.CloudEndpointTests.call

    def report(self, revision, states, counts, *, now=BEFORE_MIDNIGHT, run='a', outcome='running'):
        payload = dict(run_id=run * 32, revision=revision, states=states,
                       job_counts=counts, outcome=outcome)
        with patch.object(cloud_worker, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = now
            return self.call('groq-status', payload)

    def begin(self, run='a', slots=5):
        self.assertEqual(self.report(0, ['not_attempted'] * slots, [0] * slots, run=run).status_code, 200)
        self.assertEqual(self.report(1, ['attempting'] * slots, [0] * slots, run=run).status_code, 200)

    def totals(self):
        return [tuple(row) for row in self.conn.execute(
            'SELECT day_ist,key_slot,job_count FROM groq_daily_scores ORDER BY day_ist,key_slot')]

    def test_all_keys_accumulate_across_batches_and_idempotent_reports_do_not_double_count(self):
        for run, slots in (('a', 5), ('b', 3)):
            self.begin(run, slots)
            counts = list(range(1, slots + 1))
            for _ in range(2):
                self.assertEqual(self.report(2, ['succeeded'] * slots, counts, run=run).status_code, 200)
            self.assertEqual(self.report(3, ['succeeded'] * slots, counts, run=run,
                                         outcome='complete').status_code, 200)
        self.assertEqual(self.totals(), [('2026-11-09', 1, 2), ('2026-11-09', 2, 4),
                                         ('2026-11-09', 3, 6), ('2026-11-09', 4, 4),
                                         ('2026-11-09', 5, 5)])
        # A fresh connection sees durable totals after the endpoint transaction commits.
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute('SELECT SUM(job_count) FROM groq_daily_scores').fetchone()[0], 21)

    def test_midnight_splits_one_batch_by_report_day_and_replay_stays_on_original_day(self):
        self.begin()
        states = ['succeeded'] + ['attempting'] * 4
        self.assertEqual(self.report(2, states, [2, 0, 0, 0, 0]).status_code, 200)
        self.assertEqual(self.report(2, states, [2, 0, 0, 0, 0], now=MIDNIGHT).status_code, 200)
        self.assertEqual(self.report(3, states, [3, 0, 0, 0, 0], now=MIDNIGHT).status_code, 200)
        self.assertEqual(self.totals(), [('2026-11-09', 1, 2), ('2026-11-10', 1, 1)])
        before = monitor.snapshot(self.conn, 1, now=BEFORE_MIDNIGHT)['groq_keys']
        after = monitor.snapshot(self.conn, 1, now=MIDNIGHT)['groq_keys']
        self.assertEqual(before[0]['today_job_count'], 2)
        self.assertEqual(after[0]['today_job_count'], 1)
        self.assertTrue(all(key['today_job_count'] == 0 for key in after[1:]))

    def test_failures_status_updates_and_rejected_reports_do_not_inflate_daily_totals(self):
        self.begin()
        self.assertEqual(self.report(2, ['failed'] * 5, [0] * 5).status_code, 200)
        self.assertEqual(self.totals(), [])
        self.assertEqual(self.report(3, ['attempting'] * 5, [0] * 5).status_code, 200)
        self.assertEqual(self.report(4, ['succeeded'] * 5, [1] * 5).status_code, 200)
        previous = self.totals()
        self.assertEqual(self.report(4, ['succeeded'] * 5, [2] * 5).status_code, 409)
        self.assertEqual(self.report(6, ['succeeded'] * 5, [2] * 5).status_code, 409)
        self.assertEqual(self.report(5, ['succeeded'] * 5, [0] * 5).status_code, 400)
        invalid_states = ['succeeded'] * 4 + ['not_attempted']
        self.assertEqual(self.report(5, invalid_states, [2] * 5).status_code, 400)
        self.assertEqual(self.totals(), previous)

    def test_daily_write_failure_rolls_back_history_revision_and_counts(self):
        self.begin()
        with self.conn:
            self.conn.execute("""CREATE TRIGGER reject_daily_write BEFORE INSERT ON groq_daily_scores
                BEGIN SELECT RAISE(ABORT,'synthetic daily write failure'); END""")
        self.assertEqual(self.report(2, ['succeeded'] * 5, [1] * 5).status_code, 503)
        self.assertEqual(self.totals(), [])
        rows = self.conn.execute('SELECT revision,job_count FROM groq_key_history').fetchall()
        self.assertTrue(all(row['revision'] == 1 and row['job_count'] == 0 for row in rows))

    def test_monitor_replaces_both_columns_and_renders_zero_for_unused_keys(self):
        self.begin()
        self.assertEqual(self.report(2, ['succeeded'] * 5, [7, 0, 0, 0, 0]).status_code, 200)
        self.conn.execute('UPDATE accounts SET is_admin=1 WHERE user_id=1')
        self.conn.commit()
        with self.client.session_transaction() as session:
            session['user_id'], session['session_version'] = 1, 0
        with patch.object(monitor, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = BEFORE_MIDNIGHT
            with self.client.get('/admin/workflow-monitor') as response:
                self.assertEqual(response.status_code, 200)
                page = response.get_data(as_text=True)
        self.assertIn('<th>Scored today (IST)</th>', page)
        self.assertNotIn('Scored this batch', page)
        self.assertNotIn('Cumulative evaluations', page)
        self.assertEqual(page.count('<td class="wm-number">7</td>'), 1)
        self.assertEqual(page.count('<td class="wm-number">0</td>'), 4)


class DailyMigrationTests(unittest.TestCase):
    def test_migration_preserves_old_history_without_guessing_daily_totals(self):
        migrations = sorted((Path(__file__).parents[1] / 'backend/migrations').glob('*.sql'))
        with patch('backend.migrate.Path.glob', return_value=[path for path in migrations
                                                           if path.name != '023_groq_daily_scores.sql']):
            test_web_flows.WebFlowTests.setUp(self)
        self.conn.execute("""INSERT INTO groq_key_history
            (run_id,key_slot,state,outcome,revision,started_at,updated_at,job_count)
            VALUES(?,1,'succeeded','complete',1,'2026-11-09','2026-11-10',50)""", ('a' * 32,))
        self.conn.commit()
        self.assertIsNone(monitor.snapshot(self.conn, 1)['groq_keys'][0]['today_job_count'])
        before = [tuple(row) for row in self.conn.execute('SELECT * FROM groq_key_history')]
        apply_migrations(self.conn)
        self.conn.execute("INSERT INTO groq_daily_scores VALUES('2026-11-10',1,3)")
        self.conn.commit()
        apply_migrations(self.conn)
        self.assertEqual([tuple(row) for row in self.conn.execute('SELECT * FROM groq_key_history')], before)
        self.assertEqual([tuple(row) for row in self.conn.execute('SELECT * FROM groq_daily_scores')],
                         [('2026-11-10', 1, 3)])
