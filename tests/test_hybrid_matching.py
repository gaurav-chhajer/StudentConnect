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


class GeminiEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.user = dict(resume_text='Python project')
        self.job = dict(title='Intern', department='', location='Remote',
                        raw_json=json.dumps(dict(content='Python required')))
        self.key_patch = patch.object(pipeline, 'GEMINI_API_KEY', 'synthetic')
        self.key_patch.start()
        self.addCleanup(self.key_patch.stop)
        self.model_patch = patch.object(pipeline, 'GEMINI_MODEL', 'gemini-3.8-flash')
        self.model_patch.start()
        self.addCleanup(self.model_patch.stop)
        self.environment = patch.dict(pipeline.os.environ, GEMINI_CONTEXT_TOKENS='1048576')
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def response(self, result=None, finish='STOP'):
        return dict(candidates=[dict(finishReason=finish, content=dict(parts=[
            dict(text=json.dumps(test_evaluation.sample_result() if result is None else result))]))])

    def evaluate(self):
        return pipeline.score_job_for_user(self.user, self.job, 'Synthetic', provider='gemini')

    def test_same_rubric_and_exact_evidence_with_gemini_metadata(self):
        result = test_evaluation.sample_result()
        result['matched_requirements'] = [dict(requirement='Python', jd_line_id=1, resume_line_id=1)]
        payload = self.response(result)
        payload['candidates'][0]['content']['parts'].insert(0, dict(thought=True, text='private thoughts'))
        with patch.object(pipeline.requests, 'post') as post:
            post.return_value.json.return_value = payload
            saved = self.evaluate()
        self.assertEqual(saved['provider'], 'gemini')
        self.assertEqual(saved['model'], 'gemini-3.8-flash')
        self.assertEqual(saved['matched_requirements'][0]['jd_evidence'], 'Python required')
        self.assertEqual(saved['matched_requirements'][0]['resume_evidence'], 'Python project')
        request = post.call_args
        self.assertNotIn('synthetic', request.args[0])
        self.assertEqual(request.kwargs['headers']['x-goog-api-key'], 'synthetic')
        self.assertEqual(request.kwargs['timeout'], 30)
        self.assertFalse(request.kwargs['allow_redirects'])
        body = request.kwargs['json']
        self.assertEqual(body['systemInstruction']['parts'][0]['text'], pipeline.SCORING_PROMPT)
        self.assertIn('Python project', body['contents'][0]['parts'][0]['text'])
        self.assertEqual(body['generationConfig']['responseMimeType'], 'application/json')
        post.return_value.close.assert_called_once()

    def test_rejects_blocked_truncated_malformed_and_invalid_evidence(self):
        invalid = test_evaluation.sample_result()
        invalid['matched_requirements'] = [dict(requirement='Python', jd_line_id=99, resume_line_id=1)]
        malformed = self.response()
        malformed['candidates'][0]['content']['parts'] = [dict(text='not JSON')]
        payloads = [{}, [], dict(candidates=[]), self.response(finish='MAX_TOKENS'),
                    self.response(finish='SAFETY'), malformed, self.response(invalid),
                    self.response(dict(status='unable_to_evaluate')), self.response(dict(fit_score=100))]
        for payload in payloads:
            with self.subTest(payload_type=type(payload).__name__), patch.object(pipeline.requests, 'post') as post:
                post.return_value.json.return_value = payload
                with self.assertRaises(ValueError):
                    self.evaluate()
                post.return_value.close.assert_called_once()

    def test_http_and_network_failures_propagate_without_retry(self):
        for status in (401, 429, 503):
            with self.subTest(status=status), patch.object(pipeline.requests, 'post') as post:
                post.return_value.raise_for_status.side_effect = http_error(status)
                with self.assertRaises(requests.HTTPError):
                    self.evaluate()
                post.assert_called_once()
                post.return_value.close.assert_called_once()
        with patch.object(pipeline.requests, 'post', side_effect=requests.Timeout()) as post:
            with self.assertRaises(requests.Timeout):
                self.evaluate()
            post.assert_called_once()

    def test_invalid_input_configuration_and_budget_make_no_request(self):
        cases = [('GEMINI_API_KEY', ''), ('GEMINI_MODEL', '../private'), ('GEMINI_MODEL', 'unknown-model')]
        for field, value in cases:
            with self.subTest(field=field, value=value), patch.object(pipeline, field, value), \
                    patch.dict(pipeline.os.environ, GEMINI_CONTEXT_TOKENS=''), \
                    patch.object(pipeline.requests, 'post') as post:
                with self.assertRaises((ValueError, RuntimeError)):
                    self.evaluate()
                post.assert_not_called()
        with patch.dict(pipeline.os.environ, GEMINI_CONTEXT_TOKENS='5000'), \
                patch.object(pipeline.requests, 'post') as post:
            with self.assertRaises(ValueError):
                self.evaluate()
            post.assert_not_called()
        with patch.object(pipeline.requests, 'post') as post:
            for user, job in ((dict(resume_text=''), self.job), (self.user, dict(self.job, raw_json='{}'))):
                with self.assertRaises(ValueError):
                    pipeline.score_job_for_user(user, job, 'Synthetic', provider='gemini')
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

    def run_matching(self, score, key='synthetic'):
        client = MagicMock()
        def hosted_call(route, data=None):
            response = self.call(route, data)
            self.assertEqual(response.status_code, 200)
            return response.json
        client.call.side_effect = hosted_call
        with patch.object(pipeline, 'GROQ_API_KEY', 'synthetic'), \
                patch.object(pipeline, 'GEMINI_API_KEY', key), \
                patch.object(pipeline, 'fetch_jobs_for_company', return_value=[]), \
                patch.object(runner.time, 'monotonic', return_value=0), \
                patch.object(pipeline, 'score_job_for_user', side_effect=score) as evaluate:
            # Empty snapshots would deactivate jobs: mock only snapshot transfer in this fixture.
            original_call = client.call.side_effect
            client.call.side_effect = lambda route, data=None: ({'saved': 0} if route == 'snapshot'
                                                               else original_call(route, data))
            result = runner.fetch_and_score(client, 2100)
        return result, evaluate

    def test_rate_limit_switches_once_and_success_does_not_fail_run(self):
        result = test_evaluation.sample_result()
        status, evaluate = self.run_matching([http_error(429), result, result])
        self.assertEqual(status, 0)
        self.assertEqual([call.kwargs for call in evaluate.call_args_list], [{}, {'provider': 'gemini'}, {'provider': 'gemini'}])
        self.assertEqual(evaluate.call_args_list[0].args, evaluate.call_args_list[1].args)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM scores WHERE is_stale=0').fetchone()[0], 2)

    def test_both_limited_preserves_first_score_for_dashboard_and_email(self):
        status, evaluate = self.run_matching([test_evaluation.sample_result(), http_error(429), http_error(429)])
        self.assertEqual(status, 1)
        self.assertEqual(evaluate.call_count, 3)
        saved_job = self.conn.execute('SELECT job_id FROM scores WHERE is_stale=0').fetchone()[0]
        displayed, _ = store.matches(self.conn, 1, store.filters({}), test_cloud_worker.NOW)
        self.assertEqual([row['job_id'] for row in displayed], [saved_job])
        self.assertEqual([row['job_id'] for row in pipeline.get_unsent_top_matches(self.conn, 1)], [saved_job])
        pending = pipeline.filter_candidate_jobs(self.conn, dict(id=1, preferences_json='{}'))
        self.assertEqual(len(pending), 1)
        self.assertNotEqual(pending[0]['id'], saved_job)

    def test_no_fallback_for_other_failures_or_missing_key(self):
        for error, key in ((http_error(401), 'synthetic'), (requests.Timeout(), 'synthetic'), (http_error(429), '')):
            with self.subTest(error=type(error).__name__, configured=bool(key)):
                status, evaluate = self.run_matching([error], key)
                self.assertEqual(status, 1)
                evaluate.assert_called_once()
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM scores WHERE is_stale=0').fetchone()[0], 0)
