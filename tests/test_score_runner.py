import unittest
from unittest.mock import patch

import requests
from scripts import run_local_score as runner
from tests import test_web_flows


class ScoreRunnerTests(unittest.TestCase):
    setUp = test_web_flows.WebFlowTests.setUp
    seed_user = test_web_flows.WebFlowTests.seed_user

    def prepare(self):
        self.seed_user()
        self.conn.execute("UPDATE scores SET is_stale=1")
        self.conn.commit()

    def result(self):
        return dict(fit_score=70, reasoning="Synthetic match", matched_skills=[], missing_requirements=[])

    def test_saved_scores_are_skipped_on_rerun(self):
        self.prepare()
        with patch.object(runner.p, "score_job_for_user", return_value=self.result()) as score:
            self.assertEqual(runner.score_batch(self.conn), 0)
            self.assertEqual(score.call_count, 2)
            self.assertEqual(runner.score_batch(self.conn), 0)
            self.assertEqual(score.call_count, 2)

    def test_rate_limit_stops_and_preserves_success(self):
        self.prepare()
        response = requests.Response()
        response.status_code = 429
        response.headers["Retry-After"] = "12"
        with patch.object(runner.p, "score_job_for_user", side_effect=[self.result()] + [requests.HTTPError(response=response)] * 4) as score, \
                patch.object(runner.time, "sleep") as sleep, \
                self.assertLogs(runner.logger, level="ERROR") as logs:
            self.assertEqual(runner.score_batch(self.conn), 1)
        self.assertEqual(score.call_count, 5)
        self.assertEqual(sleep.call_count, 3)
        self.assertIn("12 seconds", " ".join(logs.output))

        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM scores WHERE is_stale=0").fetchone()[0], 1)

    def test_rate_limit_retry_recovers_and_saves_once(self):
        self.prepare()
        response = requests.Response()
        response.status_code = 429
        response.headers["Retry-After"] = "13"
        with patch.object(runner.p, "score_job_for_user", side_effect=[requests.HTTPError(response=response), self.result(), self.result()]) as score, \
                patch.object(runner.time, "sleep") as sleep, \
                patch.object(runner.p, "store_score", wraps=runner.p.store_score) as store, \
                self.assertLogs(runner.logger, level="WARNING"):
            self.assertEqual(runner.score_batch(self.conn), 0)
        sleep.assert_called_once_with(13)
        self.assertEqual(score.call_count, 3)
        self.assertEqual(score.call_args_list[0].args[1]["id"], score.call_args_list[1].args[1]["id"])
        self.assertEqual(store.call_count, 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM scores WHERE is_stale=0").fetchone()[0], 2)

    def test_invalid_or_excessive_retry_wait_stops_without_sleep(self):
        self.prepare()
        for header in (None, "", "-1", "1.5", "121", "99999999999"):
            response = requests.Response()
            response.status_code = 429
            if header is not None:
                response.headers["Retry-After"] = header
            with patch.object(runner.p, "score_job_for_user", side_effect=requests.HTTPError(response=response)) as score, \
                    patch.object(runner.time, "sleep") as sleep, self.assertLogs(runner.logger, level="ERROR"):
                self.assertEqual(runner.score_batch(self.conn), 1)
            score.assert_called_once()
            sleep.assert_not_called()

    def test_zero_retry_after_uses_exponential_floor(self):
        self.prepare()
        response = requests.Response()
        response.status_code = 429
        response.headers["Retry-After"] = "0"
        with patch.object(runner.p, "score_job_for_user", side_effect=requests.HTTPError(response=response)) as score, \
                patch.object(runner.time, "sleep") as sleep, self.assertLogs(runner.logger, level="ERROR"):
            self.assertEqual(runner.score_batch(self.conn), 1)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2, 4])
        self.assertEqual(score.call_count, 4)

    def test_network_failure_stops_without_retry(self):
        self.prepare()
        for error in (requests.ConnectionError("private payload"), requests.Timeout("private payload")):
            with patch.object(runner.p, "score_job_for_user", side_effect=error) as score, \
                    self.assertLogs(runner.logger, level="ERROR") as logs:
                self.assertEqual(runner.score_batch(self.conn), 1)
            self.assertEqual(score.call_count, 1)
            self.assertNotIn("private payload", " ".join(logs.output))

    def test_maximum_wait_uses_short_intervals(self):
        self.prepare()
        response = requests.Response()
        response.status_code = 429
        response.headers["Retry-After"] = "120"
        with patch.object(runner.p, "score_job_for_user", side_effect=[requests.HTTPError(response=response), self.result(), self.result()]), \
                patch.object(runner.time, "sleep") as sleep, self.assertLogs(runner.logger, level="WARNING"):
            self.assertEqual(runner.score_batch(self.conn), 0)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [30, 30, 30, 30])

    def test_interruption_during_wait_closes_database(self):
        self.prepare()
        response = requests.Response()
        response.status_code = 429
        response.headers["Retry-After"] = "13"
        with patch.object(runner.p, "init_db"), \
                patch.object(runner.sqlite3, "connect", return_value=self.conn), \
                patch.object(runner.p, "score_job_for_user", side_effect=requests.HTTPError(response=response)) as score, \
                patch.object(runner.time, "sleep", side_effect=KeyboardInterrupt), \
                self.assertLogs(runner.logger, level="WARNING"):
            self.assertEqual(runner.main(), 130)
        score.assert_called_once()
        with self.assertRaises(runner.sqlite3.ProgrammingError):
            self.conn.execute("SELECT 1")

    def test_http_failure_stops_before_remaining_jobs(self):
        self.prepare()
        for status, header in ((429,"private invalid header"), (401,"")):
            response = requests.Response()
            response.status_code = status
            response.headers["Retry-After"] = header
            with patch.object(runner.p, "score_job_for_user", side_effect=requests.HTTPError(response=response)) as score, \
                    self.assertLogs(runner.logger, level="ERROR") as logs:
                self.assertEqual(runner.score_batch(self.conn), 1)
            self.assertEqual(score.call_count, 1)
            self.assertNotIn("private invalid header", " ".join(logs.output))

    def test_empty_batch_without_active_users_is_reported(self):
        with self.assertLogs(runner.logger, level="ERROR"), \
                patch.object(runner.p, "score_job_for_user") as score:
            self.assertEqual(runner.score_batch(self.conn), 1)
        score.assert_not_called()

    def test_invalid_evaluation_continues_without_saving_it(self):
        self.prepare()
        with patch.object(runner.p, "score_job_for_user", side_effect=[ValueError("private evidence"), self.result()]), \
                self.assertLogs(runner.logger, level="ERROR") as logs:
            self.assertEqual(runner.score_batch(self.conn), 1)
        self.assertNotIn("private evidence", " ".join(logs.output))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM scores WHERE is_stale=0").fetchone()[0], 1)

    def test_interruption_closes_database(self):
        with patch.object(runner.p, "init_db"), patch.object(runner.sqlite3, "connect") as connect, \
                patch.object(runner, "score_batch", side_effect=KeyboardInterrupt), \
                self.assertLogs(runner.logger, level="WARNING"):
            self.assertEqual(runner.main(), 130)
        connect.return_value.close.assert_called_once()
