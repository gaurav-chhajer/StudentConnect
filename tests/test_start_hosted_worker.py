"""Background launcher tests; no external requests or real configuration reads."""

import logging
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from scripts import start_hosted_worker, watch_hosted_jobs
from scripts import run_cloud_worker as runner
from tests import test_cloud_worker, test_evaluation


class ScoringRecoveryTests(unittest.TestCase):
    def test_provider_cooldown_wait_is_interruptible_and_does_not_send_email(self):
        stop, ready = MagicMock(), MagicMock()
        stop.is_set.return_value = False
        stop.wait.return_value = True
        with patch.object(runner, 'score_pending', side_effect=watch_hosted_jobs.CooldownPending(60)) as score, \
                patch.object(runner, 'send_emails') as email:
            self.assertEqual(watch_hosted_jobs._score_loop('client', stop, ready), 0)
        stop.wait.assert_called_once_with(60)
        score.assert_called_once()
        email.assert_not_called()

    def test_failures_reconnect_after_delay_and_preserve_email_pause(self):
        for failure in (requests.ConnectionError('private'), ValueError('private'),
                        RuntimeError('private'), 1):
            with self.subTest(failure=type(failure).__name__):
                stop = threading.Event()
                clients = [MagicMock(), MagicMock()]
                states = []

                quotas = []
                def score(client, stop, ready, email_state, quota):
                    quotas.append(quota)
                    states.append(email_state)
                    if len(states) == 1:
                        email_state['paused'] = True
                        if isinstance(failure, Exception):
                            raise failure
                        return failure
                    self.assertTrue(email_state['paused'])
                    stop.set()
                    return 1

                with patch.object(watch_hosted_jobs, '_QueuedClient', side_effect=clients), \
                        patch.object(watch_hosted_jobs, '_score_loop', side_effect=score), \
                        patch.object(stop, 'wait', return_value=False) as wait, \
                        self.assertLogs(watch_hosted_jobs.logger, level='WARNING') as logs:
                    self.assertEqual(watch_hosted_jobs._scoring_supervisor(
                        threading.Lock(), stop, threading.Event()), 1)
                wait.assert_called_once_with(30)
                self.assertIs(states[0], states[1])
                self.assertIs(quotas[0], quotas[1])
                for client in clients:
                    client.close.assert_called_once()
                self.assertNotIn('private', ' '.join(logs.output))

    def test_initialization_and_cleanup_errors_wait_and_allow_shutdown(self):
        for stage in ('initialization', 'cleanup'):
            with self.subTest(stage=stage):
                stop = threading.Event()
                client = MagicMock()
                if stage == 'cleanup':
                    client.close.side_effect = OSError('private')
                with patch.object(watch_hosted_jobs, '_QueuedClient', return_value=client,
                                  side_effect=ValueError('private') if stage == 'initialization' else None) as factory, \
                        patch.object(watch_hosted_jobs, '_score_loop', return_value=0), \
                        patch.object(stop, 'wait', return_value=True) as wait, \
                        self.assertLogs(watch_hosted_jobs.logger, level='WARNING'):
                    watch_hosted_jobs._scoring_supervisor(threading.Lock(), stop, threading.Event())
                factory.assert_called_once()
                wait.assert_called_once_with(30)
                if stage == 'cleanup':
                    client.close.assert_called_once()


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
                        patch.object(watch_hosted_jobs, 'main',
                                     side_effect=lambda args: order.append(('worker', args)) or status):
                    self.assertEqual(start_hosted_worker.main(), status)
                self.assertEqual(order, [('environment', root / '.env'),
                                         ('worker', ['--run', '--provider', 'groq'])])
                self.assertIn('result_code=' + str(status), (root / 'data' / 'hosted_worker.log').read_text())
                self.assertEqual(logging.getLogger().handlers, handlers_before)
                self.assertEqual(logging.getLogger().level, level_before)

    def test_rotated_private_keys_replace_inherited_slots(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            root=Path(folder)
            (root/'data').mkdir()
            (root/'.env').write_text('GROQ_API_KEY=synthetic-new\nGROQ_API_KEY_2=synthetic-second\n'
                                     'GROQ_API_KEY_4=synthetic-fourth\nGROQ_API_KEY_5=\n')
            with patch.dict(os.environ,{'GROQ_API_KEY':'synthetic-old'}), patch.object(start_hosted_worker,'PROJECT_ROOT',root), patch.object(start_hosted_worker,'load_hosted_environment'), patch.object(watch_hosted_jobs,'main',side_effect=lambda args: 0 if os.environ['GROQ_API_KEY']=='synthetic-new' else 1):
                self.assertEqual(start_hosted_worker.main(),0)
                self.assertEqual(os.environ['GROQ_API_KEY_2'],'synthetic-second')
                self.assertEqual(os.environ['GROQ_API_KEY_4'],'synthetic-fourth')
                self.assertEqual(os.environ['GROQ_API_KEY_5'],'')

    def test_default_live_command_uses_independent_groq_queue(self):
        with patch.object(watch_hosted_jobs,'run_queued',return_value=0) as queued, patch.object(watch_hosted_jobs,'run_cycle') as sequential:
            self.assertEqual(watch_hosted_jobs.main(['--run']),0)
        queued.assert_called_once()
        sequential.assert_not_called()

    def test_startup_and_worker_failures_stop_without_logging_private_messages(self):
        for stage in ('environment', 'worker'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
                root = Path(folder)
                (root / 'data').mkdir()
                with patch.object(start_hosted_worker, 'PROJECT_ROOT', root), \
                        patch.object(start_hosted_worker, 'load_hosted_environment') as environment, \
                        patch.object(watch_hosted_jobs, 'main') as worker:
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
                                              fetch_only=True, pool=None, stop=stop, allow_partial=True)
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

        def fetch(client, deadline, *, fetch_only, pool, stop, allow_partial):
            self.assertTrue(scoring_started.wait(2))
            count.append(1)
            client.call('snapshot', {'jobs': []})
            if len(count) == 2:
                second_fetch.set()
                self.assertTrue(emailed.wait(2))
            return 0

        def score(client, deadline, stop, **kwargs):
            shared_stop.append(stop)
            scoring_started.set()
            self.assertTrue(second_fetch.wait(2))
            return 0

        def email(client, deadline, *, matches_only):
            self.assertTrue(matches_only)
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

    def test_unavailable_candidate_does_not_abort_queue_batch(self):
        client,pool=MagicMock(),MagicMock()
        client.call.side_effect=[dict(next=1,items=[dict(user=dict(id=1,resume_text='fixture'),job=dict(id=1),company_name='fixture',fingerprint='a'*64)]),dict(next=None,items=[])]
        with patch.object(runner.time,'monotonic',return_value=0), patch.object(runner.career_sources,'prepare_job',side_effect=requests.HTTPError()):
            self.assertEqual(runner._score_pending(client,1000,pool,stop=threading.Event()),0)
        pool.score.assert_not_called()

    def test_failed_scoring_does_not_stop_fetching_or_send_email(self):
        stop, ready = threading.Event(), threading.Event()
        with patch.object(runner, 'score_pending', return_value=1) as score, \
                patch.object(runner, 'send_emails') as email:
            self.assertEqual(watch_hosted_jobs._score_loop('client', stop, ready), 1)
        score.assert_called_once()
        email.assert_not_called()
        self.assertFalse(stop.is_set())

    def test_failed_email_is_not_retried_and_scoring_continues(self):
        stop, ready = threading.Event(), MagicMock()
        batches = []
        def score(*args, **kwargs):
            batches.append(1)
            if len(batches) == 2:
                stop.set()
            return 0
        with patch.object(runner, 'score_pending', side_effect=score), \
                patch.object(runner, 'send_emails', return_value=1) as email:
            self.assertEqual(watch_hosted_jobs._score_loop('client', stop, ready), 1)
        email.assert_called_once()
        self.assertEqual(len(batches), 2)
        ready.wait.assert_called_once_with(watch_hosted_jobs.QUEUE_POLL_SECONDS)

    def test_email_exceptions_pause_only_delivery_without_replaying_sends(self):
        for error in (requests.ReadTimeout('private message'), requests.ConnectionError('private message'),
                      runner.HostedOperationError(503), ValueError('private message')):
            with self.subTest(error=type(error).__name__):
                stop, ready = threading.Event(), MagicMock()
                batches = []
                def score(*args, **kwargs):
                    batches.append(1)
                    if len(batches) == 2:
                        stop.set()
                    return 0
                with patch.object(runner, 'score_pending', side_effect=score), \
                        patch.object(runner, 'send_emails', side_effect=error) as email, \
                        self.assertLogs(watch_hosted_jobs.logger, level='ERROR') as logs:
                    self.assertEqual(watch_hosted_jobs._score_loop('client', stop, ready), 1)
                self.assertEqual(len(batches), 2)
                email.assert_called_once()
                self.assertIn('Groq scoring continues', '\n'.join(logs.output))
                self.assertNotIn('private message', '\n'.join(logs.output))

    def test_read_recovery_uses_bounded_backoff_without_holding_write_lock(self):
        stop, ready, writes = MagicMock(), threading.Event(), threading.Lock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        with patch.object(runner, 'HostedClient') as factory:
            client = watch_hosted_jobs._QueuedClient(writes, stop, ready)
        factory.return_value.call.side_effect = [requests.ReadTimeout('private'),
                                                 runner.HostedOperationError(503), {'items': []}]
        def wait(delay):
            self.assertFalse(writes.locked())
            return False
        stop.wait.side_effect = wait
        with self.assertLogs(watch_hosted_jobs.logger, level='WARNING') as logs:
            self.assertEqual(client.call('work?after=0'), {'items': []})
        self.assertEqual(factory.return_value.call.call_count, 3)
        self.assertEqual([call.args[0] for call in stop.wait.call_args_list], [30, 60])
        self.assertFalse(ready.is_set())
        self.assertNotIn('private', '\n'.join(logs.output))

    def test_read_recovery_exhaustion_propagates_without_unbounded_retries(self):
        stop = MagicMock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        with patch.object(runner, 'HostedClient') as factory:
            client = watch_hosted_jobs._QueuedClient(threading.Lock(), stop, threading.Event())
        error = requests.ConnectionError('private')
        factory.return_value.call.side_effect = error
        with self.assertRaises(requests.ConnectionError) as raised:
            client.call('companies')
        self.assertIs(raised.exception, error)
        self.assertEqual(factory.return_value.call.call_count, 3)
        self.assertEqual(stop.wait.call_count, 2)

    def test_shutdown_interrupts_read_backoff_before_another_request(self):
        stop = MagicMock()
        stop.is_set.return_value = False
        stop.wait.return_value = True
        with patch.object(runner, 'HostedClient') as factory:
            client = watch_hosted_jobs._QueuedClient(threading.Lock(), stop, threading.Event())
        factory.return_value.call.side_effect = requests.ReadTimeout()
        with self.assertRaisesRegex(RuntimeError, 'stopping'):
            client.call('companies')
        factory.return_value.call.assert_called_once()
        stop.wait.assert_called_once_with(30)

    def test_nontransient_reads_and_unverified_endpoints_are_not_retried(self):
        for path, error in (('companies', runner.HostedOperationError(401)),
                            ('work?after=0', runner.HostedOperationError(403)),
                            ('companies', runner.HostedOperationError(429)),
                            ('companies', requests.exceptions.SSLError('private')),
                            ('companies', ValueError('invalid response')),
                            ('other', requests.ReadTimeout())):
            with self.subTest(path=path, error=type(error).__name__):
                stop = MagicMock()
                stop.is_set.return_value = False
                with patch.object(runner, 'HostedClient') as factory:
                    client = watch_hosted_jobs._QueuedClient(threading.Lock(), stop, threading.Event())
                factory.return_value.call.side_effect = error
                with self.assertRaises(type(error)):
                    client.call(path)
                factory.return_value.call.assert_called_once()
                stop.wait.assert_not_called()

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
        def finish_scoring(client, stop, ready, email_state, quota):
            # The supervisor owns repeated batches until explicitly stopped.
            stop.set()
            return 0

        with patch.object(watch_hosted_jobs, 'exclusive_cycle'), \
                patch.object(runner, 'HostedClient', side_effect=sessions), \
                patch.object(watch_hosted_jobs, '_fetch_loop', side_effect=ValueError('private')) as fetch, \
                patch.object(watch_hosted_jobs, '_score_loop', side_effect=finish_scoring) as score, \
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
