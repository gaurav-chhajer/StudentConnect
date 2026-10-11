import unittest
from unittest.mock import patch

import requests

from backend import pipeline_skeleton as pipeline
from scripts import run_pipeline as runner
from tests import test_web_flows


class PipelineTests(unittest.TestCase):
    def setUp(self):
        test_web_flows.WebFlowTests.setUp(self)
        for target, kwargs in (
                ("DB_PATH", {"new": str(self.database)}),
                ("GROQ_API_KEY", {"new": "synthetic-test-key"}),
                ("validate_email_configuration", {}),
                ("deliver_email", {})):
            mock = patch.object(pipeline, target, **kwargs)
            value = mock.start()
            self.addCleanup(mock.stop)
            if target == "deliver_email":
                self.mail = value
        seed = patch.object(runner.run_local_fetch, "seed_companies")
        self.seed = seed.start()
        self.addCleanup(seed.stop)
        due = patch.object(runner.run_local_email, "schedule_due", return_value=True)
        due.start()
        self.addCleanup(due.stop)

    def prepare(self):
        test_web_flows.WebFlowTests.seed_user(self)
        self.conn.execute("UPDATE scores SET is_stale=1")
        self.conn.commit()

    def fetched(self, company):
        return [dict(external_id=str(company["id"]), title="Software Engineer", location="Remote",
                     url="https://example.test/apply", raw_json={"content": "Python job"})]

    def result(self, *args):
        return dict(fit_score=85, reasoning="Synthetic evaluation", matched_skills=["Python"], missing_requirements=[])

    def test_complete_cycle_and_repeat_deduplication(self):
        self.prepare()
        with patch.object(pipeline, "fetch_jobs_for_company", side_effect=self.fetched), \
                patch.object(pipeline, "score_job_for_user", side_effect=self.result) as score:
            self.assertEqual(runner.main(["--run"]), 0)
            self.assertEqual(score.call_count, 2)
            self.assertEqual(self.mail.call_count, 1)
            self.assertEqual(len(self.mail.call_args.args[1]), 2)
            self.assertEqual(runner.main(["--run"]), 0)
            self.assertEqual(score.call_count, 2)
            self.assertEqual(self.mail.call_count, 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sent_log").fetchone()[0], 2)
        self.assertFalse((self.database.parent / "email_schedule.paused").exists())

    def test_preview_is_read_only_and_has_no_external_calls(self):
        self.prepare()
        before = self.database.read_bytes()
        with patch.object(pipeline, "fetch_jobs_for_company") as fetch, \
                patch.object(pipeline, "score_job_for_user") as score:
            self.assertEqual(runner.main([]), 0)
        fetch.assert_not_called()
        score.assert_not_called()
        self.mail.assert_not_called()
        self.seed.assert_not_called()
        self.assertEqual(before, self.database.read_bytes())

    def test_failed_board_is_excluded_but_healthy_board_continues(self):
        self.prepare()
        def fetch(company):
            if company["id"] == 1:
                raise requests.ConnectionError("private payload")
            return self.fetched(company)
        with patch.object(pipeline, "fetch_jobs_for_company", side_effect=fetch), \
                patch.object(pipeline, "score_job_for_user", side_effect=self.result) as score, \
                self.assertLogs(runner.logger, level="ERROR") as logs:
            self.assertEqual(runner.main(["--run"]), 1)
        self.assertEqual(score.call_count, 1)
        self.assertEqual([row["job_id"] for row in self.mail.call_args.args[1]], [2])
        self.assertEqual(self.conn.execute("SELECT is_active FROM jobs WHERE id=1").fetchone()[0], 1)
        self.assertNotIn("private payload", " ".join(logs.output))

    def test_all_boards_failed_stops_before_scoring_and_email(self):
        self.prepare()
        with patch.object(pipeline, "fetch_jobs_for_company", side_effect=requests.Timeout), \
                patch.object(pipeline, "score_job_for_user") as score, self.assertLogs(runner.logger, level="ERROR"):
            self.assertEqual(runner.main(["--run"]), 1)
        score.assert_not_called()
        self.mail.assert_not_called()

    def test_successful_empty_board_closes_old_jobs(self):
        self.prepare()
        with patch.object(pipeline, "fetch_jobs_for_company", return_value=[]), \
                patch.object(pipeline, "score_job_for_user") as score:
            self.assertEqual(runner.main(["--run"]), 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM jobs WHERE is_active=1").fetchone()[0], 0)
        score.assert_not_called()
        self.mail.assert_not_called()

    def test_observed_partial_empty_repeat_and_conflict_preserve_jobs(self):
        self.prepare()
        self.conn.execute("UPDATE companies SET career_source='wipro' WHERE id=1")
        self.conn.commit()
        posting = self.fetched(dict(id=1))[0]
        with patch.object(pipeline.matching_tasks, 'enqueue_company') as enqueue:
            self.assertEqual(pipeline.upsert_jobs(self.conn, 1, [posting, dict(posting)]), ['1'])
            self.assertEqual(enqueue.call_count, 1)
            self.assertEqual(pipeline.upsert_jobs(self.conn, 1, [posting]), [])
            self.assertEqual(pipeline.upsert_jobs(self.conn, 1, []), [])
            self.assertEqual(enqueue.call_count, 1)
            with self.assertLogs('backend.job_ingestion', level='WARNING'):
                self.assertEqual(pipeline.upsert_jobs(self.conn, 1, [posting, dict(posting, title='Other role')]), [])
            self.assertEqual(enqueue.call_count, 1)
        self.assertEqual(self.conn.execute('SELECT is_active FROM jobs WHERE id=1').fetchone()[0], 1)
        with patch.object(pipeline, 'fetch_jobs_for_company', return_value=[]):
            runner.fetch_cycle(self.conn, {1})
        self.assertEqual(self.conn.execute('SELECT is_active FROM jobs WHERE id=1').fetchone()[0], 1)

    def test_budget_defers_remaining_scores_until_next_cycle(self):
        self.prepare()
        with patch.object(pipeline, "fetch_jobs_for_company", side_effect=self.fetched), \
                patch.object(pipeline, "score_job_for_user", side_effect=self.result) as score:
            self.assertEqual(runner.main(["--run", "--max-scores", "1"]), 0)
            self.assertEqual(score.call_count, 1)
            self.assertEqual(runner.main(["--run", "--max-scores", "1"]), 0)
            self.assertEqual(score.call_count, 2)

    def test_unverified_inactive_and_empty_resume_profiles_are_not_scored(self):
        self.prepare()
        for statement in ("UPDATE accounts SET email_verified=0", "UPDATE users SET active=0",
                          "UPDATE users SET resume_text=''"):
            with self.subTest(statement=statement):
                self.conn.execute("UPDATE accounts SET email_verified=1")
                self.conn.execute("UPDATE users SET active=1,resume_text='Python resume'")
                self.conn.execute(statement)
                self.conn.commit()
                with patch.object(pipeline, "fetch_jobs_for_company", side_effect=self.fetched), \
                        patch.object(pipeline, "score_job_for_user") as score:
                    self.assertEqual(runner.main(["--run"]), 0)
                score.assert_not_called()

    def test_delivery_failure_pauses_future_cycles_and_preserves_scores(self):
        self.prepare()
        self.mail.side_effect = RuntimeError("private provider detail")
        with patch.object(pipeline, "fetch_jobs_for_company", side_effect=self.fetched) as fetch, \
                patch.object(pipeline, "score_job_for_user", side_effect=self.result), \
                self.assertLogs(runner.logger, level="ERROR") as logs:
            self.assertEqual(runner.main(["--run"]), 1)
            fetch.reset_mock()
            self.assertEqual(runner.main(["--run"]), 1)
            fetch.assert_not_called()
        self.assertTrue((self.database.parent / "email_schedule.paused").is_file())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM scores WHERE is_stale=0").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sent_log").fetchone()[0], 0)
        self.assertEqual(self.mail.call_count, 1)
        self.assertNotIn("private provider detail", " ".join(logs.output))

    def test_lock_blocks_overlapping_cycle_and_releases_after_error(self):
        path = self.database.parent / "pipeline.lock"
        with runner.exclusive_cycle(path):
            with self.assertRaises(RuntimeError):
                with runner.exclusive_cycle(path):
                    self.fail("Overlapping lock was admitted")
        with self.assertRaises(ValueError):
            with runner.exclusive_cycle(path):
                raise ValueError("Synthetic error")
        with runner.exclusive_cycle(path):
            self.assertTrue(path.exists())

    def test_not_due_and_off_preferences_do_not_send(self):
        self.prepare()
        with patch.object(pipeline, "fetch_jobs_for_company", side_effect=self.fetched), \
                patch.object(pipeline, "score_job_for_user", side_effect=self.result), \
                patch.object(runner.run_local_email, "schedule_due", return_value=False):
            self.assertEqual(runner.main(["--run"]), 0)
        self.mail.assert_not_called()
        self.conn.execute("INSERT INTO user_settings(user_id,cadence) VALUES(1,'off')")
        self.conn.commit()
        with patch.object(pipeline, "fetch_jobs_for_company", side_effect=self.fetched):
            self.assertEqual(runner.main(["--run"]), 0)
        self.mail.assert_not_called()

    def test_input_change_during_groq_request_discards_result(self):
        self.prepare()
        def evaluate(*args):
            self.conn.execute("UPDATE users SET resume_text='Updated resume'")
            self.conn.commit()
            return self.result()
        with patch.object(pipeline, "score_job_for_user", side_effect=evaluate), \
                self.assertLogs(runner.logger, level="WARNING"):
            self.assertEqual(runner.score_cycle(self.conn, {1, 2}, 1), 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM scores WHERE is_stale=0").fetchone()[0], 0)

    def test_missing_config_fails_before_any_network_call(self):
        self.prepare()
        with patch.object(pipeline, "GROQ_API_KEY", ""), \
                patch.object(pipeline, "fetch_jobs_for_company") as fetch, self.assertLogs(runner.logger, level="ERROR"):
            self.assertEqual(runner.main(["--run"]), 1)
        fetch.assert_not_called()
        self.mail.assert_not_called()

    def test_filtered_email_selection_applies_before_top_five_limit(self):
        test_web_flows.WebFlowTests.seed_user(self)
        for number in range(3, 8):
            self.conn.execute("""INSERT INTO jobs(id,company_id,external_id,title,raw_json,first_seen_at,last_seen_at)
                VALUES(?,1,?,'Python developer','{}','2026-10-01','2026-10-01')""", (number, str(number)))
            pipeline.store_score(self.conn, number, 1, dict(fit_score=99, reasoning="Synthetic"))
        self.conn.commit()
        matches = pipeline.get_unsent_top_matches(self.conn, 1, company_ids={2})
        self.assertEqual([row["job_id"] for row in matches], [2])
        self.assertEqual(pipeline.get_unsent_top_matches(self.conn, 1, company_ids=set()), [])
        with self.assertRaises(ValueError):
            pipeline.get_unsent_top_matches(self.conn, 1, company_ids={"invalid"})

    def test_new_accounts_and_preferences_are_picked_up_on_next_cycle(self):
        self.prepare()
        self.conn.execute("INSERT INTO user_settings(user_id,subscriptions_configured) VALUES(1,1)")
        self.conn.execute("INSERT INTO company_subscriptions(user_id,company_id,subscribed_at) VALUES(1,1,'2026-10-07T00:00:00+00:00')")
        self.conn.execute("INSERT INTO users(id,name,email,resume_text) VALUES(2,'Tester','tester@example.test','SQL resume')")
        self.conn.execute("INSERT INTO accounts(user_id,password_hash,created_at) VALUES(2,'synthetic','2026-10-07')")
        self.conn.execute("INSERT INTO user_settings(user_id,subscriptions_configured) VALUES(2,1)")
        self.conn.execute("INSERT INTO company_subscriptions(user_id,company_id,subscribed_at) VALUES(2,2,'2026-10-07T00:00:00+00:00')")
        self.conn.commit()
        with patch.object(pipeline, "fetch_jobs_for_company", side_effect=self.fetched), \
                patch.object(pipeline, "score_job_for_user", side_effect=self.result) as score:
            self.assertEqual(runner.main(["--run"]), 0)
        self.assertEqual([(call.args[0]["id"], call.args[1]["company_id"]) for call in score.call_args_list], [(2, 2), (1, 1)])
        self.assertEqual(self.mail.call_count, 2)
        self.assertEqual([(call.args[0]["id"], call.args[1][0]["job_id"]) for call in self.mail.call_args_list], [(1, 1), (2, 2)])

    def test_groq_failure_preserves_and_sends_only_valid_completed_scores(self):
        self.prepare()
        with patch.object(pipeline, "fetch_jobs_for_company", side_effect=self.fetched), \
                patch.object(runner.run_local_score, "score_with_rate_limit_retries",
                             side_effect=[self.result(), requests.Timeout("private payload")]), \
                self.assertLogs(runner.logger, level="ERROR") as logs:
            self.assertEqual(runner.main(["--run"]), 1)
        self.assertEqual([row["job_id"] for row in self.mail.call_args.args[1]], [1])
        self.assertEqual(self.conn.execute("SELECT is_stale FROM scores WHERE job_id=2").fetchone()[0], 1)
        self.assertNotIn("private payload", " ".join(logs.output))

    def test_interrupted_delivery_keeps_pause_but_releases_cycle_lock(self):
        self.prepare()
        self.mail.side_effect = KeyboardInterrupt
        with patch.object(pipeline, "fetch_jobs_for_company", side_effect=self.fetched), \
                patch.object(pipeline, "score_job_for_user", side_effect=self.result), \
                self.assertLogs(runner.logger, level="WARNING"):
            self.assertEqual(runner.main(["--run"]), 130)
        self.assertTrue((self.database.parent / "email_schedule.paused").exists())
        with runner.exclusive_cycle(self.database.parent / "pipeline.lock"):
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sent_log").fetchone()[0], 0)

    def test_no_accounts_is_a_safe_noop(self):
        with patch.object(pipeline, "fetch_jobs_for_company") as fetch:
            self.assertEqual(runner.main(["--run"]), 0)
        fetch.assert_not_called()
        self.mail.assert_not_called()

    def test_cli_rejects_invalid_budgets(self):
        for budget in ("0", "101", "not-an-integer"):
            with self.subTest(budget=budget), self.assertRaises(SystemExit), patch("sys.stderr"):
                runner.main(["--max-scores", budget])

    def test_malformed_board_response_is_not_treated_as_zero_openings(self):
        for ats in ("greenhouse", "lever", "ashby"):
            with self.subTest(ats=ats), patch.object(pipeline.requests, "get") as request:
                request.return_value.json.return_value = {"error": "Unavailable"}
                with self.assertRaises(ValueError):
                    pipeline.fetch_jobs_for_company(dict(ats_type=ats, board_slug="synthetic"))
                request.return_value.json.return_value = [] if ats == "lever" else {"jobs": []}
                self.assertEqual(pipeline.fetch_jobs_for_company(dict(ats_type=ats, board_slug="synthetic")), [])
