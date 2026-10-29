"""Local flow tests: real temporary SQLite, deterministic embeddings, mocked SMTP/ATS."""

import json
import smtplib
import sqlite3
import threading
import unittest
from contextlib import closing
from unittest.mock import MagicMock, patch

import requests

from backend import local_embeddings, pipeline_skeleton as pipeline, similarity
from scripts import local_worker
from tests import test_local_similarity, test_web_flows


class LocalFlowTests(unittest.TestCase):
    def setUp(self):
        test_web_flows.WebFlowTests.setUp(self)
        test_web_flows.WebFlowTests.seed_user(self)
        self.writes, self.stop, self.ready = threading.Lock(), threading.Event(), threading.Event()
        self.model = test_local_similarity.Model()
        self.scorer = local_embeddings.LocalScorer(self.model, self.database.parent / 'cache.db')
        self.addCleanup(self.scorer.close)
        sender = patch.object(pipeline, 'deliver_account_email')
        self.sender = sender.start()
        self.addCleanup(sender.stop)
        credentials = patch.multiple(pipeline, GMAIL_APP_PASSWORD='a' * 16, DIGEST_FROM_EMAIL='sender@example.test')
        credentials.start()
        self.addCleanup(credentials.stop)

    def score(self):
        return local_worker.score_batch(self.database, self.writes, self.stop, self.scorer)

    def send(self):
        return local_worker.send_digests(self.database, self.writes, self.stop)

    def ranked(self, scores):
        for job_id, score in scores.items():
            job = dict(self.conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
            user = dict(self.conn.execute('SELECT * FROM users WHERE id=1').fetchone())
            # The fixture has two JD skills, one of which is in the resume.
            value = similarity.result((score / 100 - 0.15) / 0.7, user['resume_text'], pipeline.extract_job_description(job))
            self.assertTrue(local_worker.save_result(self.database, self.writes, self.conn, user, job, job, value))

    def test_fetch_to_offline_scoring_to_email_then_restart_without_duplicates(self):
        posting = dict(external_id='new-local', title='Software Engineer', location='Remote',
                       url='https://example.test/new', raw_json={'content': 'Python and SQL'})
        with patch.object(pipeline, 'fetch_jobs_for_company', return_value=[posting]), \
                patch.object(pipeline, 'score_job_for_user', side_effect=AssertionError('Groq must not be called')):
            local_worker.fetch_cycle(self.database, self.writes, self.stop, self.ready)
            self.assertTrue(self.ready.is_set())
            self.assertEqual(self.score(), 2)
            self.assertEqual(self.send(), 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM local_similarity').fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sent_log WHERE channel='email'").fetchone()[0], 2)
        text = self.sender.call_args.args[2]
        self.assertIn('Local similarity:', text)
        self.assertIn('not a fit percentage', text)
        # Reopen all worker storage as a restarted process would.
        with closing(local_embeddings.LocalScorer(self.model, self.database.parent / 'cache.db')) as scorer:
            self.assertEqual(local_worker.score_batch(self.database, self.writes, self.stop, scorer), 0)
        self.assertEqual(self.send(), 0)
        self.sender.assert_called_once()

    def test_failed_board_retains_jobs_and_does_not_block_successful_board(self):
        posting = dict(external_id='new', title='Software Engineer', raw_json={'content': 'Python SQL'})
        with patch.object(pipeline, 'fetch_jobs_for_company', side_effect=[requests.Timeout('private'), [posting]]):
            local_worker.fetch_cycle(self.database, self.writes, self.stop, self.ready)
        self.assertEqual(self.conn.execute('SELECT is_active FROM jobs WHERE id=1').fetchone()[0], 1)
        self.assertIsNotNone(self.conn.execute("SELECT id FROM jobs WHERE company_id=2 AND external_id='new'").fetchone())

    def test_invalid_snapshot_never_replaces_saved_jobs(self):
        for invalid in ([dict(external_id='bad', title='', raw_json={})],
                        [dict(external_id='bad', title='Engineer', url='javascript:alert(1)', raw_json={})]):
            with self.subTest(invalid=invalid), patch.object(pipeline, 'fetch_jobs_for_company', return_value=invalid):
                local_worker.fetch_cycle(self.database, self.writes, self.stop, self.ready)
            self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM jobs WHERE is_active=1').fetchone()[0], 2)

    def test_changed_jobs_and_preferences_reenter_queue(self):
        self.assertEqual(self.score(), 2)
        self.assertEqual(self.score(), 0)
        self.conn.execute("UPDATE jobs SET raw_json=? WHERE id=1", (json.dumps({'content': 'Python SQL revised requirements'}),))
        self.conn.commit()
        self.assertEqual(self.score(), 1)
        self.conn.execute("UPDATE users SET preferences_json=? WHERE id=1", (json.dumps({'locations': ['Remote']}),))
        self.conn.commit()
        self.assertEqual(self.score(), 2)

    def test_slow_inference_rejects_changed_inputs_but_not_fetch_timestamps(self):
        for changed in ('resume', 'timestamp'):
            with self.subTest(changed=changed):
                def infer(user, job, company):
                    if changed == 'resume':
                        self.conn.execute("UPDATE users SET resume_text='Changed Python resume' WHERE id=1")
                    else:
                        self.conn.execute("UPDATE jobs SET last_seen_at='2026-10-29T00:00:00+00:00' WHERE id=?", (job['id'],))
                    self.conn.commit()
                    return similarity.result(.9, user['resume_text'], pipeline.extract_job_description(job))
                with patch.object(self.scorer, 'score', side_effect=infer), patch.object(local_worker, 'MAX_EVALUATIONS', 1):
                    self.assertEqual(self.score(), int(changed == 'timestamp'))

    def test_budget_leaves_pending_jobs_for_next_batch(self):
        with patch.object(local_worker, 'MAX_EVALUATIONS', 1):
            self.assertEqual(self.score(), 1)
            self.assertEqual(self.score(), 1)
            self.assertEqual(self.score(), 0)

    def test_threshold_boundary_and_existing_sent_history(self):
        self.ranked({1: 59.99, 2: 60})
        self.assertEqual(self.send(), 1)
        self.assertEqual(self.conn.execute('SELECT job_id FROM sent_log').fetchone()[0], 2)
        self.assertEqual(self.send(), 0)
        self.ranked({1: 61})
        self.conn.execute("INSERT INTO sent_log(user_id,job_id,channel,sent_at) VALUES(1,1,'email','2026-10-29T00:00:00+00:00')")
        self.conn.commit()
        self.assertEqual(self.send(), 0)

    def test_off_unverified_inactive_and_stale_profiles_are_not_emailed(self):
        self.ranked({1: 80, 2: 80})
        self.conn.execute("INSERT INTO user_settings(user_id,cadence) VALUES(1,'off')")
        self.conn.commit()
        self.assertEqual(self.send(), 0)
        self.conn.execute("UPDATE user_settings SET cadence='daily' WHERE user_id=1")
        self.conn.execute('UPDATE accounts SET email_verified=0 WHERE user_id=1')
        self.conn.commit()
        self.assertEqual(self.send(), 0)
        self.conn.execute('UPDATE accounts SET email_verified=1 WHERE user_id=1')
        self.conn.execute("UPDATE users SET resume_text='Updated resume' WHERE id=1")
        self.conn.commit()
        self.assertEqual(self.send(), 0)
        self.sender.assert_not_called()

    def test_subscriptions_and_include_gaps_are_honored(self):
        self.ranked({1: 80, 2: 80})
        self.conn.execute('INSERT INTO user_settings(user_id,subscriptions_configured,include_gaps) VALUES(1,1,0)')
        self.conn.execute("INSERT INTO company_subscriptions(user_id,company_id,subscribed_at) VALUES(1,2,'2026-10-29T00:00:00+00:00')")
        self.conn.commit()
        self.assertEqual(self.send(), 1)
        self.assertEqual([row[0] for row in self.conn.execute('SELECT job_id FROM sent_log')], [2])
        self.assertNotIn('JD skills not mentioned', self.sender.call_args.args[2])

    def test_uncertain_smtp_or_recording_failure_leaves_pause_and_never_retries(self):
        self.ranked({1: 80})
        self.sender.side_effect = smtplib.SMTPServerDisconnected('private failure')
        with self.assertRaises(smtplib.SMTPServerDisconnected):
            self.send()
        self.assertTrue((self.database.parent / 'email_schedule.paused').is_file())
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM sent_log').fetchone()[0], 0)
        with self.assertRaises(RuntimeError):
            self.send()
        self.sender.assert_called_once()

    def test_smtp_acceptance_without_persisted_history_stays_paused(self):
        self.ranked({1: 80})
        with patch.object(pipeline, 'mark_sent', side_effect=sqlite3.OperationalError('private')):
            with self.assertRaises(sqlite3.OperationalError):
                self.send()
        self.assertTrue((self.database.parent / 'email_schedule.paused').exists())
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM sent_log').fetchone()[0], 0)

    def test_invalid_results_or_thresholds_are_rejected_before_sending(self):
        self.ranked({1: 80})
        self.conn.execute('UPDATE local_similarity SET rank_score=100 WHERE job_id=1')
        self.conn.commit()
        with self.assertRaises(ValueError):
            self.send()
        self.assertFalse((self.database.parent / 'email_schedule.paused').exists())
        for threshold in (True, -1, 101, float('nan'), '60'):
            with self.subTest(threshold=threshold), self.assertRaises(ValueError):
                local_worker.send_digests(self.database, self.writes, self.stop, threshold)
        self.sender.assert_not_called()

    def test_stopped_worker_does_no_inference_or_email(self):
        self.stop.set()
        with patch.object(self.scorer, 'score') as score:
            self.assertEqual(self.score(), 0)
        score.assert_not_called()
        self.assertEqual(self.send(), 0)

    def test_fetch_loop_uses_start_time_and_handles_overrun_without_overlap(self):
        for elapsed, delay in ((20, 280), (450, 0)):
            stop = MagicMock()
            stop.is_set.side_effect = [False, True]
            with patch.object(local_worker, 'fetch_cycle') as fetch, \
                    patch.object(local_worker.time, 'monotonic', side_effect=[100, 100 + elapsed]):
                local_worker.fetch_loop(self.database, self.writes, stop, self.ready)
            fetch.assert_called_once()
            stop.wait.assert_called_once_with(delay)

    def test_email_failure_does_not_stop_scoring_or_repeat_sends(self):
        stop = MagicMock()
        stop.is_set.side_effect = [False, False, False, True]
        with patch.object(local_worker, 'LocalScorer', return_value=self.scorer), \
                patch.object(local_worker, 'score_batch', return_value=1) as score, \
                patch.object(local_worker, 'send_digests', side_effect=RuntimeError('private')) as send, \
                patch.object(self.ready, 'wait'):
            local_worker.matching_loop(self.database, self.writes, stop, self.ready, 60)
        self.assertEqual(score.call_count, 2)
        send.assert_called_once()

    def test_loop_failure_is_reported_and_other_loop_still_runs(self):
        with patch.object(local_worker, 'fetch_loop', side_effect=RuntimeError('private')) as fetch, \
                patch.object(local_worker, 'matching_loop') as matching, \
                self.assertLogs(local_worker.logger) as logs:
            self.assertEqual(local_worker.run(self.database), 1)
        fetch.assert_called_once()
        matching.assert_called_once()
        self.assertNotIn('private', ' '.join(logs.output))

    def test_fetch_runs_again_while_local_inference_is_busy(self):
        scoring, fetched_twice, emailed = threading.Event(), threading.Event(), threading.Event()
        fetches = []

        def fetch(database, writes, stop, ready):
            self.assertTrue(scoring.wait(2))
            fetches.append(1)
            ready.set()
            if len(fetches) == 2:
                fetched_twice.set()
                self.assertTrue(emailed.wait(2))

        def score(database, writes, stop, scorer):
            scoring.set()
            self.assertTrue(fetched_twice.wait(2))
            return 1

        def send(database, writes, stop, minimum):
            stop.set()
            emailed.set()
            return 1

        with patch.object(local_worker, 'LocalScorer', return_value=MagicMock()), \
                patch.object(local_worker, 'FETCH_INTERVAL_SECONDS', 0), \
                patch.object(local_worker, 'fetch_cycle', side_effect=fetch), \
                patch.object(local_worker, 'score_batch', side_effect=score), \
                patch.object(local_worker, 'send_digests', side_effect=send):
            self.assertEqual(local_worker.run(self.database), 0)
        self.assertEqual(len(fetches), 2)
        self.assertTrue(emailed.is_set())
