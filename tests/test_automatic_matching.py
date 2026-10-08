import unittest
from logging.handlers import RotatingFileHandler
from unittest.mock import patch

from backend import automatic_matching, matching_tasks
from scripts import run_pipeline as runner
from tests import test_pipeline, test_web_flows


class AutomaticMatchingTests(unittest.TestCase):
    def setUp(self):
        test_pipeline.PipelineTests.setUp(self)
        def close_logs():
            for target in (automatic_matching.logger, runner.logger):
                for handler in list(target.handlers):
                    if isinstance(handler, RotatingFileHandler) and handler.baseFilename == str(self.database.parent / 'matching.log'):
                        target.removeHandler(handler)
                        handler.close()
        self.addCleanup(close_logs)

    prepare = test_pipeline.PipelineTests.prepare
    fetched = test_pipeline.PipelineTests.fetched
    result = test_pipeline.PipelineTests.result
    post = test_web_flows.WebFlowTests.post
    settings_data = test_web_flows.WebFlowTests.settings_data

    def test_main_accepts_database_path_and_locks_beside_it(self):
        self.prepare()
        self.post('/settings', self.settings_data(companies=['1'], roles='', locations=''))
        with patch.object(runner.pipeline, 'DB_PATH', '/unused/database.db'), \
                patch.object(runner, 'exclusive_cycle', wraps=runner.exclusive_cycle) as lock, \
                patch.object(runner.pipeline, 'fetch_jobs_for_company', side_effect=self.fetched), \
                patch.object(runner.pipeline, 'score_job_for_user', side_effect=self.result):
            self.assertEqual(runner.main(['--run', '--matching-only'], database_path=self.database), 0)
        lock.assert_called_once_with(self.database.parent / 'pipeline.lock')
        self.assertEqual(matching_tasks.status(self.conn, 1)['status'], 'complete')
        self.mail.assert_not_called()

    def test_type_error_does_not_stop_loop_and_normal_wait_is_used(self):
        service = automatic_matching.MatchingService(self.database)
        def wait(seconds):
            if cycle.call_count == 2:
                service.stop.set()
        with patch.object(service, 'cycle', side_effect=[TypeError('unexpected keyword'), 0]) as cycle, \
                patch.object(service.wake, 'wait', side_effect=wait) as waiting, \
                self.assertLogs(automatic_matching.logger, level='ERROR') as logs:
            service.run()
        self.assertEqual(cycle.call_count, 2)
        self.assertEqual(waiting.call_count, 2)
        waiting.assert_called_with(automatic_matching.POLL_SECONDS)
        self.assertIn('TypeError', logs.output[0])
        self.assertIn('unexpected keyword', logs.output[0])
        self.assertIsNotNone(logs.records[0].exc_info)

    def test_rotating_log_records_cycle_progress(self):
        self.prepare()
        self.post('/settings', self.settings_data(companies=['1'], roles='', locations=''))
        service = automatic_matching.MatchingService(self.database)
        automatic_matching.configure_logging(self.database)
        handlers = [h for h in automatic_matching.logger.handlers if isinstance(h, RotatingFileHandler)]
        self.assertEqual(len(handlers), 1)
        self.assertGreater(handlers[0].maxBytes, 0)
        self.assertGreater(handlers[0].backupCount, 0)
        with patch.object(runner.pipeline, 'fetch_jobs_for_company', side_effect=self.fetched), \
                patch.object(runner.pipeline, 'score_job_for_user', side_effect=self.result):
            self.assertEqual(service.cycle(), 0)
        log = (self.database.parent / 'matching.log').read_text()
        self.assertIn('cycle started', log)
        self.assertIn('Matching tasks claimed: 1', log)
        self.assertIn('result_code=0', log)

    def test_web_save_wakes_service_after_commit(self):
        self.prepare()
        self.app.testing = False
        states = []

        def start(database, notify=False):
            self.assertEqual(database, self.database)
            if notify:
                states.append(matching_tasks.status(self.conn, 1)['status'])

        with patch.object(automatic_matching, 'ensure_started', side_effect=start):
            self.assertEqual(self.post('/settings', self.settings_data(companies=['1'])).status_code, 302)
        self.assertEqual(states, ['pending'])

    def test_service_uses_web_database_and_never_sends_mail(self):
        self.prepare()
        self.post('/settings', self.settings_data(companies=['1'], roles='', locations=''))
        service = automatic_matching.MatchingService(self.database)
        with patch.object(runner.pipeline, 'DB_PATH', '/unused/database.db'), \
                patch.object(runner.pipeline, 'fetch_jobs_for_company', side_effect=self.fetched) as fetch, \
                patch.object(runner.pipeline, 'score_job_for_user', side_effect=self.result):
            self.assertEqual(service.cycle(), 0)
            self.assertEqual(matching_tasks.status(self.conn, 1)['status'], 'complete')
            self.assertEqual(service.cycle(), 0)
        self.assertEqual(fetch.call_count, 1)
        self.mail.assert_not_called()

    def test_service_continues_budget_limited_work(self):
        self.prepare()
        self.post('/settings', self.settings_data(companies=['1'], roles='', locations=''))
        service = automatic_matching.MatchingService(self.database)
        jobs = [dict(external_id=str(i), title='Software Engineer', location='Remote',
                     raw_json={'content': 'Python job'}) for i in range(8)]
        with patch.object(runner.pipeline, 'fetch_jobs_for_company', return_value=jobs), \
                patch.object(runner.pipeline, 'score_job_for_user', side_effect=self.result) as score:
            self.assertEqual(service.cycle(), 0)
            self.assertEqual(matching_tasks.status(self.conn, 1)['status'], 'pending')
            self.assertEqual(service.cycle(), 0)
        self.assertEqual(score.call_count, 8)
        self.assertEqual(matching_tasks.status(self.conn, 1)['status'], 'complete')

    def test_standalone_upsert_queues_new_postings(self):
        self.prepare()
        runner.pipeline.upsert_jobs(self.conn, 1, self.fetched({'id': 1}))
        self.assertEqual(matching_tasks.status(self.conn, 1)['status'], 'pending')
        revision = self.conn.execute('SELECT revision FROM matching_tasks').fetchone()[0]
        runner.pipeline.upsert_jobs(self.conn, 1, self.fetched({'id': 1}))
        self.assertEqual(self.conn.execute('SELECT revision FROM matching_tasks').fetchone()[0], revision)

    def test_lock_contention_leaves_tasks_available(self):
        self.prepare()
        service = automatic_matching.MatchingService(self.database)
        with runner.exclusive_cycle(self.database.parent / 'pipeline.lock'), \
                patch.object(runner.pipeline, 'fetch_jobs_for_company') as fetch:
            with self.assertRaises(RuntimeError):
                service.cycle()
        fetch.assert_not_called()
        self.assertEqual(service.next_refresh, 0)

    def test_posting_queue_failure_rolls_back_snapshot(self):
        self.prepare()
        before = self.conn.execute('SELECT * FROM jobs ORDER BY id').fetchall()
        with patch.object(matching_tasks, 'enqueue_company', side_effect=ValueError('Queue failed')):
            with self.assertRaises(ValueError):
                runner.pipeline.upsert_jobs(self.conn, 1, self.fetched({'id': 1}))
        self.assertEqual(self.conn.execute('SELECT * FROM jobs ORDER BY id').fetchall(), before)
