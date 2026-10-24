import os
import smtplib
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from backend import cloud_worker, match_reminders, pipeline_skeleton as pipeline
from backend.migrate import apply_migrations
from tests import test_web_flows

NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)
KEY = 'ab' * 32  # Synthetic test credential only.


class MatchReminderTests(unittest.TestCase):
    def setUp(self):
        test_web_flows.WebFlowTests.setUp(self)
        test_web_flows.WebFlowTests.seed_user(self)
        self.conn.execute("INSERT INTO user_settings(user_id,min_score) VALUES(1,100)")
        self.conn.commit()
        self.client = self.app.test_client()
        self.environment = patch.dict(os.environ, STUDENTCONNECT_WORKER_KEY=KEY,
                                      STUDENTCONNECT_BASE_URL='https://example.test')
        self.environment.start()
        self.addCleanup(self.environment.stop)
        smtp = patch.object(pipeline.smtplib, 'SMTP_SSL', side_effect=AssertionError('Unexpected SMTP'))
        smtp.start()
        self.addCleanup(smtp.stop)

    def due(self, now=NOW):
        return match_reminders.due(self.conn, 1, now)

    def call(self):
        response = self.client.post('/internal/worker/email/immediate', json=dict(after=0),
                                    headers={'Authorization': 'Bearer ' + KEY})
        self.addCleanup(response.close)
        self.addCleanup(response.request.input_stream.close)
        return response

    def test_first_reminder_at_seven_days_and_only_one_followup_after_fourteen(self):
        self.assertFalse(self.due(NOW - timedelta(microseconds=1)))
        self.assertTrue(self.due())
        match_reminders.mark_sent(self.conn, 1, NOW)
        self.assertFalse(self.due(NOW + timedelta(days=14, microseconds=-1)))
        self.assertTrue(self.due(NOW + timedelta(days=14)))
        match_reminders.mark_sent(self.conn, 1, NOW + timedelta(days=14))
        self.assertFalse(self.due(NOW + timedelta(days=365)))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM sent_log').fetchone()[0], 0)

    def test_last_job_email_is_the_reference_and_resets_exhausted_reminders(self):
        self.assertTrue(self.due())
        match_reminders.mark_sent(self.conn, 1, NOW)
        match_reminders.mark_sent(self.conn, 1, NOW + timedelta(days=14))
        latest = NOW + timedelta(days=15)
        self.conn.execute("INSERT INTO sent_log(user_id,job_id,channel,sent_at) VALUES(1,1,'email',?)",
                          (latest.isoformat(),))
        self.assertFalse(self.due(latest))
        self.assertFalse(self.due(latest + timedelta(days=7, microseconds=-1)))
        self.assertTrue(self.due(latest + timedelta(days=7)))
        self.assertEqual(self.conn.execute('SELECT sent_count FROM match_reminders').fetchone()[0], 0)

    def test_profile_changes_restart_the_window(self):
        for statement in ("UPDATE users SET resume_text='Updated Python resume'",
                          "UPDATE users SET preferences_json='{} '",
                          "UPDATE user_settings SET min_score=99",
                          "UPDATE user_settings SET subscriptions_configured=1",
                          "INSERT INTO company_subscriptions VALUES(1,1,'2026-10-08T00:00:00+00:00')"):
            with self.subTest(statement=statement):
                self.conn.execute('DELETE FROM match_reminders')
                self.assertTrue(self.due())
                match_reminders.mark_sent(self.conn, 1, NOW)
                self.conn.execute(statement)
                self.assertFalse(self.due(NOW + timedelta(days=1)))
                self.assertTrue(self.due(NOW + timedelta(days=8)))

    def test_off_inactive_unverified_and_missing_users_are_excluded(self):
        self.conn.execute("UPDATE user_settings SET cadence='off'")
        self.assertFalse(self.due())
        self.conn.execute("UPDATE user_settings SET cadence='daily'")
        self.conn.execute('UPDATE users SET active=0')
        self.assertFalse(self.due())
        self.conn.execute('UPDATE users SET active=1')
        self.conn.execute('UPDATE accounts SET email_verified=0')
        self.assertFalse(self.due())
        self.assertFalse(match_reminders.due(self.conn, 999, NOW))

    def test_pending_failed_and_unscored_work_blocks_reminders(self):
        self.conn.execute("INSERT INTO matching_tasks(user_id,reason,updated_at) VALUES(1,'resume',?)", (NOW.isoformat(),))
        for state in ('pending', 'running', 'failed'):
            self.conn.execute('UPDATE matching_tasks SET status=?', (state,))
            self.assertFalse(self.due())
        self.conn.execute("UPDATE matching_tasks SET status='complete'")
        self.conn.execute('UPDATE scores SET is_stale=1')
        self.assertFalse(self.due())
        self.conn.execute('UPDATE scores SET is_stale=0')
        self.assertTrue(self.due())

    def test_qualifying_unsent_match_has_priority(self):
        self.conn.execute('UPDATE user_settings SET min_score=60')
        self.assertFalse(self.due())

    def test_invalid_clocks_and_state_fail_closed(self):
        with self.assertRaises(ValueError):
            self.due(NOW.replace(tzinfo=None))
        self.conn.execute("UPDATE accounts SET created_at='2026-10-09T00:00:00+00:00'")
        self.assertFalse(self.due())
        self.conn.execute("UPDATE accounts SET created_at='2026-10-01T00:00:00'")
        with self.assertRaises(ValueError):
            self.due()
        with self.assertRaises(RuntimeError):
            match_reminders.mark_sent(self.conn, 1, NOW)

    def test_content_has_action_link_and_off_instructions(self):
        user = dict(email='owner@example.test')
        with patch.object(pipeline, 'deliver_account_email') as delivery:
            match_reminders.deliver(user)
        recipient, subject, text = delivery.call_args.args
        self.assertEqual(recipient, user['email'])
        self.assertIn('opportunity', subject)
        self.assertIn('https://example.test/settings', text)
        self.assertIn('turn emails Off', text)
        self.assertIn('minimum fit-score', text)
        with patch.dict(os.environ, STUDENTCONNECT_BASE_URL=''):
            with self.assertRaises(ValueError):
                match_reminders.deliver(user)

    def test_hosted_reminder_delivery_deduplicates_and_preserves_job_log(self):
        with patch.object(cloud_worker, 'datetime') as clock, \
                patch.object(pipeline, 'validate_email_configuration'), \
                patch.object(pipeline, 'deliver_account_email') as mail:
            clock.now.return_value = NOW
            self.assertTrue(self.call().json['sent'])
            self.assertFalse(self.call().json['sent'])
        mail.assert_called_once()
        self.assertEqual(self.conn.execute('SELECT sent_count FROM match_reminders').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM sent_log').fetchone()[0], 0)
        self.assertFalse((self.database.parent / 'email_schedule.paused').exists())

    def test_uncertain_reminder_pauses_delivery_without_retry(self):
        with patch.object(cloud_worker, 'datetime') as clock, \
                patch.object(pipeline, 'validate_email_configuration'), \
                patch.object(pipeline, 'deliver_account_email', side_effect=smtplib.SMTPServerDisconnected()) as mail:
            clock.now.return_value = NOW
            self.assertEqual(self.call().status_code, 503)
            self.assertEqual(self.call().status_code, 503)
        mail.assert_called_once()
        self.assertEqual(self.conn.execute('SELECT sent_count FROM match_reminders').fetchone()[0], 0)
        self.assertTrue((self.database.parent / 'email_schedule.paused').exists())

    def test_additive_migration_is_idempotent_and_preserves_state(self):
        self.assertTrue(self.due())
        match_reminders.mark_sent(self.conn, 1, NOW)
        self.conn.commit()
        apply_migrations(self.conn)
        self.assertEqual(self.conn.execute('SELECT sent_count FROM match_reminders').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM users').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
