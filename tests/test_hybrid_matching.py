import copy
import json
import unittest
from unittest.mock import MagicMock, patch

import requests

from backend import cloud_worker, pipeline_skeleton as pipeline, store
from scripts import run_cloud_worker as runner
from tests import test_cloud_worker, test_evaluation


def http_error(status):
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError('private provider error', response=response)


class GroqEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.user = dict(resume_text='Python project')
        self.job = dict(title='Intern', department='', location='Remote',
                        raw_json=json.dumps(dict(content='Python required')))
        env = patch.dict(pipeline.os.environ, GROQ_CONTEXT_TOKENS='131072')
        env.start()
        self.addCleanup(env.stop)

    def evaluate(self):
        return pipeline.score_job_for_user(self.user, self.job, 'Synthetic', api_key='synthetic-slot-2')

    def test_selected_key_uses_same_rubric_and_exact_evidence(self):
        result = test_evaluation.sample_result()
        result['matched_requirements'] = [dict(requirement='Python', jd_line_id=1, resume_line_id=1)]
        with patch.object(pipeline.requests, 'post') as post:
            post.return_value.json.return_value = {'choices': [{'message': {'content': json.dumps(result)}}]}
            saved = self.evaluate()
        self.assertEqual(saved['provider'], 'groq')
        self.assertEqual(saved['model'], pipeline.GROQ_MODEL)
        self.assertEqual(saved['matched_requirements'][0]['jd_evidence'], 'Python required')
        self.assertEqual(saved['matched_requirements'][0]['resume_evidence'], 'Python project')
        request = post.call_args
        self.assertEqual(request.args[0], 'https://api.groq.com/openai/v1/chat/completions')
        self.assertEqual(request.kwargs['headers']['Authorization'], 'Bearer synthetic-slot-2')
        self.assertEqual(request.kwargs['timeout'], 30)
        self.assertEqual(request.kwargs['json']['messages'][0]['content'], pipeline.SCORING_PROMPT)
        post.return_value.close.assert_called_once()
        self.assertNotIn('synthetic-slot-2', json.dumps(saved))

    def test_invalid_outputs_and_evidence_remain_rejected(self):
        invalid = test_evaluation.sample_result()
        invalid['matched_requirements'] = [dict(requirement='Python', jd_line_id=99, resume_line_id=1)]
        for content in ('not JSON', '{}', json.dumps(invalid), json.dumps(dict(status='unable_to_evaluate'))):
            with self.subTest(content_type=type(content).__name__), patch.object(pipeline.requests, 'post') as post:
                post.return_value.json.return_value = {'choices': [{'message': {'content': content}}]}
                with self.assertRaises(ValueError):
                    self.evaluate()
                post.return_value.close.assert_called_once()

    def test_http_and_network_errors_propagate_without_internal_retry(self):
        for status in (401, 429, 503):
            with self.subTest(status=status), patch.object(pipeline.requests, 'post') as post:
                post.return_value.raise_for_status.side_effect = http_error(status)
                with self.assertRaises(requests.HTTPError):
                    self.evaluate()
                post.assert_called_once()
                post.return_value.close.assert_called_once()
        with patch.object(pipeline.requests, 'post', side_effect=requests.ReadTimeout()) as post:
            with self.assertRaises(requests.ReadTimeout):
                self.evaluate()
            post.assert_called_once()

    def test_invalid_input_and_budget_make_no_request(self):
        with patch.object(pipeline.requests, 'post') as post:
            for user, job, key in ((dict(resume_text=''), self.job, 'synthetic'),
                                   (self.user, dict(self.job, raw_json='{}'), 'synthetic'),
                                   (self.user, self.job, '')):
                with self.assertRaises((ValueError, RuntimeError)):
                    pipeline.score_job_for_user(user, job, 'Synthetic', api_key=key)
            with patch.dict(pipeline.os.environ, GROQ_CONTEXT_TOKENS='5000'):
                with self.assertRaises(ValueError):
                    self.evaluate()
            post.assert_not_called()


class HybridEndpointTests(unittest.TestCase):
    setUp = test_cloud_worker.CloudEndpointTests.setUp
    call = test_cloud_worker.CloudEndpointTests.call

    def test_ranks_before_five_item_limit_and_preserves_unselected_jobs(self):
        self.conn.execute("UPDATE users SET resume_text='Python Flask SQL' WHERE id=1")
        self.conn.execute("UPDATE jobs SET title='General Intern',raw_json=?", (json.dumps(dict(content='General tasks')),))
        original = self.conn.execute('SELECT * FROM jobs WHERE id=1').fetchone()
        for number in range(3, 9):
            self.conn.execute("""INSERT INTO jobs(company_id,external_id,title,url,raw_json,first_seen_at,last_seen_at)
                VALUES(1,?,?,?,?,?,?)""", (str(number), 'Python Intern' if number == 8 else 'General Intern',
                original['url'], json.dumps(dict(content='Flask SQL' if number == 8 else 'General tasks')),
                original['first_seen_at'], original['last_seen_at']))
        self.conn.commit()
        with patch.object(pipeline.requests, 'post') as external:
            payload = self.call('work').json
        external.assert_not_called()
        self.assertEqual(len(payload['items']), 5)
        self.assertEqual(payload['items'][0]['job']['id'], 8)
        self.assertEqual([item['job']['id'] for item in payload['items'][1:]], [1, 2, 3, 4])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM jobs WHERE is_active=1').fetchone()[0], 8)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM scores WHERE is_stale=0').fetchone()[0], 0)

    def test_keywords_are_distinct_and_missing_description_remains_eligible(self):
        user = dict(resume_text='Python Python SQL')
        jobs = [dict(id=2, title='Intern', department=None, raw_json=json.dumps(dict(content='Python ' * 20))),
                dict(id=1, title='Intern', department='', raw_json=json.dumps(dict(content='SQL Python'))),
                dict(id=3, title='Python Intern', department='', raw_json='{}')]
        before = copy.deepcopy(jobs)
        with self.app.app_context(), self.assertLogs(self.app.logger, level='WARNING') as logs:
            ranked = cloud_worker.rank_candidates(jobs, user)
        self.assertEqual([job['id'] for job in ranked], [3, 1, 2])
        self.assertEqual(jobs, before)
        self.assertIn('job_id=3', ' '.join(logs.output))
        self.assertNotIn('Python', ' '.join(logs.output))
        with self.app.app_context():
            self.assertEqual(cloud_worker.rank_candidates([], user), [])
            self.assertEqual([job['id'] for job in cloud_worker.rank_candidates(jobs[:2], dict(resume_text=''))], [1, 2])

    def run_matching(self, score, keys=('synthetic-2', 'synthetic-3'), clock=None):
        client = MagicMock()
        def hosted_call(route, data=None):
            if route == 'snapshot':
                return {'saved': 0}  # Keep fixture postings active for real score writes.
            response = self.call(route, data)
            self.assertEqual(response.status_code, 200, response.json)
            return response.json
        client.call.side_effect = hosted_call
        with patch.object(pipeline, 'GROQ_API_KEY', 'synthetic-1'), \
                patch.dict(pipeline.os.environ, GROQ_API_KEY_2=keys[0], GROQ_API_KEY_3=keys[1]), \
                patch.object(pipeline, 'fetch_jobs_for_company', return_value=[]), \
                patch.object(runner.time, 'monotonic', return_value=0, side_effect=clock), \
                patch.object(pipeline, 'score_job_for_user', side_effect=score) as evaluate:
            result = runner.fetch_and_score(client, 2100)
        return result, evaluate

    def test_rate_limits_advance_in_order_and_keep_using_third_key(self):
        result = test_evaluation.sample_result()
        status, evaluate = self.run_matching([http_error(429), http_error(429), result, result])
        self.assertEqual(status, 0)
        self.assertEqual([call.kwargs for call in evaluate.call_args_list],
                         [{}, {'api_key': 'synthetic-2'}, {'api_key': 'synthetic-3'}, {'api_key': 'synthetic-3'}])
        self.assertEqual(evaluate.call_args_list[0].args, evaluate.call_args_list[2].args)
        rows = self.conn.execute('SELECT key_slot,state,outcome FROM groq_key_history ORDER BY key_slot').fetchall()
        self.assertEqual([row['state'] for row in rows], ['rate_limited', 'rate_limited', 'succeeded'])
        self.assertTrue(all(row['outcome'] == 'complete' for row in rows))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM scores WHERE is_stale=0').fetchone()[0], 2)
        self.assertNotIn('synthetic', repr([dict(row) for row in self.conn.execute('SELECT * FROM groq_key_history')]))

    def test_all_limited_preserves_saved_score_for_dashboard_and_email(self):
        status, evaluate = self.run_matching([test_evaluation.sample_result()] + [http_error(429)] * 3)
        self.assertEqual(status, 1)
        self.assertEqual(evaluate.call_count, 4)
        saved_job = self.conn.execute('SELECT job_id FROM scores WHERE is_stale=0').fetchone()[0]
        displayed, _ = store.matches(self.conn, 1, store.filters({}), test_cloud_worker.NOW)
        self.assertEqual([row['job_id'] for row in displayed], [saved_job])
        self.assertEqual([row['job_id'] for row in pipeline.get_unsent_top_matches(self.conn, 1)], [saved_job])
        pending = pipeline.filter_candidate_jobs(self.conn, dict(id=1, preferences_json='{}'))
        self.assertEqual(len(pending), 1)
        rows = self.conn.execute('SELECT state,outcome FROM groq_key_history').fetchall()
        self.assertTrue(all(row['state'] == 'rate_limited' and row['outcome'] == 'deferred' for row in rows))

    def test_missing_slot_is_skipped_and_each_run_restarts_at_key_one(self):
        status, evaluate = self.run_matching([http_error(429), test_evaluation.sample_result(),
                                             test_evaluation.sample_result()], keys=('', 'synthetic-3'))
        self.assertEqual(status, 0)
        self.assertEqual(evaluate.call_args_list[1].kwargs, {'api_key': 'synthetic-3'})
        self.conn.execute('UPDATE scores SET is_stale=1')
        self.conn.commit()
        status, evaluate = self.run_matching([test_evaluation.sample_result()] * 2)
        self.assertEqual(status, 0)
        self.assertEqual(evaluate.call_args_list[0].kwargs, {})
        self.assertEqual(self.conn.execute('SELECT COUNT(DISTINCT run_id) FROM groq_key_history').fetchone()[0], 2)

    def test_other_failures_do_not_switch_keys(self):
        for error in (http_error(401), http_error(503), requests.ReadTimeout()):
            with self.subTest(error=type(error).__name__):
                status, evaluate = self.run_matching([error])
                self.assertEqual(status, 1)
                evaluate.assert_called_once()
        rows = self.conn.execute('SELECT state FROM groq_key_history WHERE key_slot=2').fetchall()
        self.assertTrue(all(row['state'] == 'not_attempted' for row in rows))

    def test_rejected_evaluation_does_not_switch_and_next_candidate_can_succeed(self):
        status, evaluate = self.run_matching([ValueError('private evaluation'), test_evaluation.sample_result()])
        self.assertEqual(status, 1)
        self.assertEqual([call.kwargs for call in evaluate.call_args_list], [{}, {}])
        self.assertEqual(self.conn.execute("SELECT state FROM groq_key_history WHERE key_slot=1").fetchone()[0], 'succeeded')

    def test_deadline_prevents_next_key_request(self):
        clock = {'now': 0}
        def limited(*args, **kwargs):
            clock['now'] = 2100 - runner.GROQ_EVALUATION_RESERVE
            raise http_error(429)
        status, evaluate = self.run_matching(limited, clock=lambda: clock['now'])
        self.assertEqual(status, 1)
        evaluate.assert_called_once()
        self.assertEqual(self.conn.execute('SELECT state FROM groq_key_history WHERE key_slot=2').fetchone()[0], 'not_attempted')

    def test_no_secondary_keys_stops_after_first_limit(self):
        status, evaluate = self.run_matching([http_error(429)], keys=('', ''))
        self.assertEqual(status, 1)
        evaluate.assert_called_once()

    def test_duplicate_keys_rejected_without_status_or_provider_calls(self):
        with patch.object(pipeline, 'GROQ_API_KEY', 'synthetic'), \
                patch.dict(pipeline.os.environ, GROQ_API_KEY_2='synthetic', GROQ_API_KEY_3=''), \
                patch.object(pipeline, 'score_job_for_user') as evaluate:
            client = MagicMock()
            with self.assertRaises(ValueError):
                runner.fetch_and_score(client, 2100)
            client.call.assert_not_called()
            evaluate.assert_not_called()
