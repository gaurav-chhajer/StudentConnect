"""Background launcher tests; no external requests or real configuration reads."""

import logging
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from scripts import start_hosted_worker, watch_hosted_jobs
from scripts import run_cloud_worker as runner
from tests import test_cloud_worker, test_evaluation


class StartupWorkerTests(unittest.TestCase):
    def test_loads_environment_before_full_worker_and_preserves_exit_status(self):
        for status in (0, 1, 130):
            with self.subTest(status=status), tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
                root = Path(folder)
                (root / 'data').mkdir()
                order = []
                handlers_before = list(logging.getLogger().handlers)
                level_before = logging.getLogger().level
                with patch.object(start_hosted_worker, 'PROJECT_ROOT', root), \
                        patch.object(start_hosted_worker, 'load_hosted_environment',
                                     side_effect=lambda path: order.append(('environment', path))), \
                        patch.object(watch_hosted_jobs, 'run_queued',
                                     side_effect=lambda: order.append(('worker',)) or status):
                    self.assertEqual(start_hosted_worker.main(), status)
                self.assertEqual(order, [('environment', root / '.env'),
                                         ('worker',)])
                self.assertIn('result_code=' + str(status), (root / 'data' / 'hosted_worker.log').read_text())
                self.assertEqual(logging.getLogger().handlers, handlers_before)
                self.assertEqual(logging.getLogger().level, level_before)

    def test_startup_and_worker_failures_stop_without_logging_private_messages(self):
        for stage in ('environment', 'worker'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
                root = Path(folder)
                (root / 'data').mkdir()
                with patch.object(start_hosted_worker, 'PROJECT_ROOT', root), \
                        patch.object(start_hosted_worker, 'load_hosted_environment') as environment, \
                        patch.object(watch_hosted_jobs, 'run_queued') as worker:
                    failing = environment if stage == 'environment' else worker
                    failing.side_effect = ValueError('private failure detail')
                    self.assertEqual(start_hosted_worker.main(), 1)
                    failing.assert_called_once()
                    if stage == 'environment':
                        worker.assert_not_called()
                log = (root / 'data' / 'hosted_worker.log').read_text()
                self.assertIn('ValueError', log)
                self.assertNotIn('private failure detail', log)


class QueuedWorkerTests(unittest.TestCase):
    def test_fetch_cadence_uses_start_time_and_does_not_overlap_long_fetches(self):
        for duration, expected_delay in ((20, 280), (300, 0), (450, 0)):
            with self.subTest(duration=duration):
                stop = MagicMock()
                stop.is_set.return_value = False
                stop.wait.return_value = True
                with patch.object(runner, '_fetch_and_score', return_value=0) as fetch, \
                        patch.object(watch_hosted_jobs.time, 'monotonic', side_effect=[100, 100 + duration]):
                    self.assertEqual(watch_hosted_jobs._fetch_loop('client', stop), 0)
                fetch.assert_called_once_with('client', 100 + runner.MATCHING_SECONDS,
                                              fetch_only=True, pool=None, stop=stop)
                stop.wait.assert_called_once_with(expected_delay)

    def test_fetch_failure_preserves_queue_and_does_not_restart_uncertain_writes(self):
        stop = threading.Event()
        with patch.object(runner, '_fetch_and_score', return_value=1) as fetch:
            self.assertEqual(watch_hosted_jobs._fetch_loop('client', stop), 1)
        fetch.assert_called_once()
        self.assertFalse(stop.is_set())

    def test_real_loops_fetch_again_while_scoring_is_still_busy(self):
        scoring_started, second_fetch, emailed = threading.Event(), threading.Event(), threading.Event()
        count = []
        sessions = [MagicMock(), MagicMock()]
        shared_stop = []

        def fetch(client, deadline, *, fetch_only, pool, stop):
            self.assertTrue(scoring_started.wait(2))
            count.append(1)
            client.call('snapshot', {'jobs': []})
            if len(count) == 2:
                second_fetch.set()
                self.assertTrue(emailed.wait(2))
            return 0

        def score(client, deadline, stop):
            shared_stop.append(stop)
            scoring_started.set()
            self.assertTrue(second_fetch.wait(2))
            return 0

        def email(client, deadline, *, immediate):
            self.assertTrue(immediate)
            shared_stop[0].set()
            emailed.set()
            return 0

        with patch.object(watch_hosted_jobs, 'exclusive_cycle'), \
                patch.object(watch_hosted_jobs, 'CHECK_INTERVAL_SECONDS', 0), \
                patch.object(runner, 'HostedClient', side_effect=sessions), \
                patch.object(runner, '_fetch_and_score', side_effect=fetch), \
                patch.object(runner, 'score_pending', side_effect=score), \
                patch.object(runner, 'send_emails', side_effect=email):
            self.assertEqual(watch_hosted_jobs.run_queued(), 0)
        self.assertEqual(len(count), 2)
        self.assertTrue(emailed.is_set())
        for session in sessions:
            session.close.assert_called_once()

    def test_failed_scoring_does_not_stop_fetching_or_send_email(self):
        stop, ready = threading.Event(), threading.Event()
        with patch.object(runner, 'score_pending', return_value=1) as score, \
                patch.object(runner, 'send_emails') as email:
            self.assertEqual(watch_hosted_jobs._score_loop('client', stop, ready), 1)
        score.assert_called_once()
        email.assert_not_called()
        self.assertFalse(stop.is_set())

    def test_failed_email_is_not_retried(self):
        stop, ready = threading.Event(), threading.Event()
        with patch.object(runner, 'score_pending', return_value=0), \
                patch.object(runner, 'send_emails', return_value=1) as email:
            self.assertEqual(watch_hosted_jobs._score_loop('client', stop, ready), 1)
        email.assert_called_once()

    def test_clients_serialize_writes_signal_only_accepted_snapshots_and_release_on_error(self):
        writes, stop, ready = threading.Lock(), threading.Event(), threading.Event()
        with patch.object(runner, 'HostedClient') as factory:
            client = watch_hosted_jobs._QueuedClient(writes, stop, ready)
        session = factory.return_value

        def call(path, data=None):
            self.assertEqual(writes.locked(), data is not None)
            return {'saved': 1}

        session.call.side_effect = call
        self.assertEqual(client.call('work?after=0'), {'saved': 1})
        self.assertFalse(ready.is_set())
        client.call('snapshot', {'jobs': []})
        self.assertTrue(ready.is_set())
        ready.clear()
        session.call.side_effect = requests.Timeout('private write')
        with self.assertRaises(requests.Timeout):
            client.call('snapshot', {'jobs': []})
        self.assertFalse(writes.locked())
        self.assertFalse(ready.is_set())
        stop.set()
        session.call.reset_mock()
        with self.assertRaises(RuntimeError):
            client.call('score', {})
        session.call.assert_not_called()
        client.close()
        session.close.assert_called_once()

    def test_write_lock_timeout_makes_no_request(self):
        writes = MagicMock()
        writes.acquire.return_value = False
        with patch.object(runner, 'HostedClient') as factory:
            client = watch_hosted_jobs._QueuedClient(writes, threading.Event(), threading.Event())
        with self.assertRaises(RuntimeError):
            client.call('snapshot', {})
        writes.acquire.assert_called_once_with(timeout=watch_hosted_jobs.WRITE_LOCK_SECONDS)
        factory.return_value.call.assert_not_called()
        writes.release.assert_not_called()
        client.close()

    def test_loop_exception_is_observed_and_other_loop_still_runs(self):
        sessions = [MagicMock(), MagicMock()]
        with patch.object(watch_hosted_jobs, 'exclusive_cycle'), \
                patch.object(runner, 'HostedClient', side_effect=sessions), \
                patch.object(watch_hosted_jobs, '_fetch_loop', side_effect=ValueError('private')) as fetch, \
                patch.object(watch_hosted_jobs, '_score_loop', return_value=0) as score, \
                self.assertLogs(watch_hosted_jobs.logger) as logs:
            self.assertEqual(watch_hosted_jobs.run_queued(), 1)
        fetch.assert_called_once()
        score.assert_called_once()
        self.assertNotIn('private', ' '.join(logs.output))
        for session in sessions:
            session.close.assert_called_once()


class QueuedScoringTests(unittest.TestCase):
    def item(self, job_id=1):
        return dict(user=dict(id=1, resume_text='Synthetic Python resume'),
                    job=dict(id=job_id, company_id=1, raw_json='{}'),
                    company_name='Synthetic', fingerprint='0' * 64)

    def test_scores_persisted_queue_without_refetching(self):
        item = self.item()
        client = MagicMock()
        client.call.side_effect = [dict(next=1, items=[item]), dict(saved=True), dict(next=None, items=[])]
        with patch.object(runner, 'GroqKeyPool', return_value=MagicMock(reporting_failed=False)) as pool, \
                patch.object(runner.career_sources, 'prepare_job', return_value=item['job']), \
                patch.object(runner.time, 'monotonic', return_value=0):
            self.assertEqual(runner.score_pending(client, 3000, threading.Event()), 0)
        self.assertEqual([call.args[0] for call in client.call.call_args_list],
                         ['work?after=0', 'score', 'work?after=1'])
        pool.return_value.score.assert_called_once()
        pool.return_value.publish.assert_called_once_with('complete')

    def test_empty_queue_is_a_successful_batch(self):
        client = MagicMock()
        client.call.return_value = dict(next=None, items=[])
        with patch.object(runner, 'GroqKeyPool', return_value=MagicMock(reporting_failed=False)) as pool, \
                patch.object(runner.time, 'monotonic', return_value=0):
            self.assertEqual(runner.score_pending(client, 3000, threading.Event()), 0)
        pool.return_value.score.assert_not_called()

    def test_queue_budget_keeps_backlog_for_another_batch(self):
        items = [self.item(number) for number in (1, 2)]
        client = MagicMock()
        client.call.side_effect = [dict(next=1, items=items), dict(saved=True)]
        with patch.object(runner, 'GroqKeyPool', return_value=MagicMock(reporting_failed=False)) as pool, \
                patch.object(runner, 'MAX_SCORES', 1), \
                patch.object(runner.career_sources, 'prepare_job', side_effect=lambda job, deadline: job), \
                patch.object(runner.time, 'monotonic', return_value=0):
            self.assertEqual(runner.score_pending(client, 3000, threading.Event()), 0)
        pool.return_value.score.assert_called_once()
        self.assertEqual(client.call.call_count, 2)

    def test_changed_fingerprint_is_discarded_but_uncertain_save_stops(self):
        for failure, expected in ((runner.HostedInputsChanged('stale'), 0),
                                  (requests.Timeout('private'), 1),
                                  (runner.HostedOperationError(503), 1)):
            with self.subTest(failure=type(failure).__name__):
                item = self.item()
                client = MagicMock()
                client.call.side_effect = [dict(next=1, items=[item]), failure, dict(next=None, items=[])]
                with patch.object(runner, 'GroqKeyPool', return_value=MagicMock(reporting_failed=False)) as pool, \
                        patch.object(runner.career_sources, 'prepare_job', return_value=item['job']), \
                        patch.object(runner.time, 'monotonic', return_value=0):
                    self.assertEqual(runner.score_pending(client, 3000, threading.Event()), expected)
                self.assertEqual(sum(call.args[0] == 'score' for call in client.call.call_args_list), 1)
                pool.return_value.publish.assert_called_once_with('deferred' if expected else 'complete')

    def test_invalid_queue_batch_stops_without_scoring(self):
        for payload in ({'items': []}, {'next': None, 'items': [self.item()]},
                        {'next': 1, 'items': [dict(self.item(), fingerprint='invalid')]}):
            with self.subTest(payload=payload):
                client = MagicMock()
                client.call.return_value = payload
                with patch.object(runner, 'GroqKeyPool', return_value=MagicMock(reporting_failed=False)) as pool, \
                        patch.object(runner.time, 'monotonic', return_value=0):
                    with self.assertRaises(ValueError):
                        runner.score_pending(client, 3000, threading.Event())
                pool.return_value.score.assert_not_called()

    def test_cancellation_stops_before_reading_queue(self):
        stop = threading.Event()
        stop.set()
        client = MagicMock()
        with patch.object(runner, 'GroqKeyPool', return_value=MagicMock(reporting_failed=False)) as pool:
            self.assertEqual(runner.score_pending(client, 3000, stop), 1)
        client.call.assert_not_called()
        pool.return_value.score.assert_not_called()
        pool.return_value.publish.assert_not_called()


class DurableQueueTests(unittest.TestCase):
    def setUp(self):
        test_cloud_worker.CloudEndpointTests.setUp(self)

    def call(self, route, data=None):
        response = test_cloud_worker.CloudEndpointTests.call(self, route, data)
        self.assertEqual(response.status_code, 200)
        return response.json

    def test_saved_snapshot_survives_client_restart_and_completed_scores_leave_queue(self):
        company = self.call('companies')['companies'][0]
        snapshot = dict(company_id=1, fingerprint=company['fingerprint'], jobs=[dict(
            external_id='queued', title='Software Engineer', location='Remote',
            url='https://example.test/apply', raw_json={'descriptionPlain': 'Python and SQL'})])
        self.assertEqual(self.call('snapshot', snapshot), {'saved': 1})
        queued_id = self.conn.execute("SELECT id FROM jobs WHERE external_id='queued'").fetchone()[0]
        # A new HTTP client models a restarted worker: all work comes from SQLite.
        self.client = self.app.test_client()
        self.assertIn(queued_id, [item['job']['id'] for item in self.call('work')['items']])
        client = MagicMock()
        client.call.side_effect = self.call
        pool = MagicMock(reporting_failed=False)
        pool.score.return_value = test_evaluation.sample_result()
        with patch.object(runner, 'GroqKeyPool', return_value=pool):
            self.assertEqual(runner.score_pending(client, runner.time.monotonic() + 3000,
                                                 threading.Event()), 0)
        self.client = self.app.test_client()
        self.assertEqual(self.call('work')['items'], [])
        self.assertEqual(self.call('snapshot', snapshot), {'saved': 1})
        self.assertEqual(self.call('work')['items'], [])
        snapshot['jobs'][0]['raw_json']['descriptionPlain'] = 'Python and SQL; revised requirements'
        self.call('snapshot', snapshot)
        self.assertEqual([item['job']['id'] for item in self.call('work')['items']], [queued_id])
