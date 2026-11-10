import json
import os
import copy
import unittest
import threading
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import requests

from backend import tavily_search
from scripts import run_cloud_worker as runner
from tests import test_cloud_worker, test_evaluation


class TavilySearchTests(unittest.TestCase):
    def test_configured_key_slots_skip_blanks_and_reject_duplicates(self):
        with patch.dict(os.environ, {
                'TAVILY_API_KEY': 'key-one', 'TAVILY_API_KEY_2': '', 'TAVILY_API_KEY_3': 'key-three'}):
            self.assertEqual(runner.configured_tavily_keys(), ('key-one', 'key-three'))
        with patch.dict(os.environ, {
                'TAVILY_API_KEY': 'same-key', 'TAVILY_API_KEY_2': 'same-key', 'TAVILY_API_KEY_3': ''}):
            with self.assertRaises(ValueError):
                runner.configured_tavily_keys()

    def response(self, payload):
        response = MagicMock()
        response.status_code = 200
        response.iter_content.return_value = [json.dumps(payload).encode()]
        response.__enter__.return_value = response
        return response

    def test_search_returns_deduplicated_leads_and_bounds_request(self):
        response = self.response({'results': [{'url': 'https://example.test/job'},
                                               {'url': 'https://example.test/job'}]})
        with patch.object(tavily_search.requests, 'post', return_value=response) as post:
            self.assertEqual(tavily_search.search('Software engineer India', 'synthetic'),
                             ['https://example.test/job'])
        self.assertEqual(post.call_args.kwargs['json']['search_depth'], 'basic')
        self.assertEqual(post.call_args.kwargs['json']['max_results'], 10)
        self.assertFalse(post.call_args.kwargs['allow_redirects'])
        response.__exit__.assert_called_once()

    def test_rejects_invalid_input_and_responses(self):
        for query, key in [('', 'key'), ('x' * 401, 'key'), ('job', '')]:
            with self.subTest(query=query), self.assertRaises(ValueError):
                tavily_search.search(query, key)
        for payload in ({}, {'results': [None]}, {'results': [{'url': 'http://example.test'}]},
                        {'results': [{'url': 'https://user@example.test'}]}):
            with self.subTest(payload=payload), patch.object(tavily_search.requests, 'post',
                                                            return_value=self.response(payload)):
                with self.assertRaises(ValueError):
                    tavily_search.search('job', 'key')

    def test_network_failure_propagates(self):
        with patch.object(tavily_search.requests, 'post', side_effect=requests.Timeout):
            with self.assertRaises(requests.Timeout):
                tavily_search.search('job', 'key')

    def test_search_rejects_redirect_oversized_malformed_and_exhausted_budget(self):
        for status, chunks in ((302, []), (200, [b'not json']),
                               (200, [b'x' * (tavily_search.MAX_RESPONSE_BYTES + 1)])):
            response = self.response({})
            response.status_code, response.iter_content.return_value = status, chunks
            with patch.object(tavily_search.requests, 'post', return_value=response), self.assertRaises(ValueError):
                tavily_search.search('job', 'synthetic')
            response.__exit__.assert_called_once()
        with patch.object(tavily_search.time, 'monotonic', return_value=100), \
                patch.object(tavily_search.requests, 'post') as post, self.assertRaises(ValueError):
            tavily_search.search('job', 'synthetic', deadline=100)
        post.assert_not_called()


class PostingDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.company = dict(id=1, name='Alpha', ats_type='lever', board_slug='alpha')
        self.url = 'https://jobs.lever.co/alpha/one'
        self.record = {'@type': 'JobPosting', 'title': 'Software Engineer',
                       'hiringOrganization': {'name': 'Alpha'}, 'description': '<p>Python and SQL</p>',
                       'jobLocation': {'address': {'addressLocality': 'Pune', 'addressCountry': 'India'}},
                       'employmentType': 'FULL_TIME', 'validThrough': '2027-01-01T00:00:00Z'}
        clock = patch.object(tavily_search.time, 'monotonic', return_value=100)
        clock.start()
        self.addCleanup(clock.stop)
        dates = patch.object(tavily_search, 'datetime', wraps=datetime)
        dates.start().now.return_value = datetime(2026, 11, 8, tzinfo=timezone.utc)
        self.addCleanup(dates.stop)

    def response(self, record):
        response = MagicMock(status_code=200)
        response.__enter__.return_value = response
        response.iter_content.return_value = [('<script type="application/ld+json">' +
            json.dumps(record) + '</script>').encode()]
        return response

    def test_verifies_full_description_and_keeps_query_identity(self):
        response = self.response(self.record)
        with patch.object(tavily_search.requests, 'get', return_value=response) as get:
            posting = tavily_search.verify_posting(self.url + '?job=1#top', self.company, 200)
        self.assertEqual(posting['title'], 'Software Engineer')
        self.assertEqual(posting['location'], 'Pune, India')
        self.assertEqual(posting['url'], self.url + '?job=1')
        self.assertEqual(posting['raw_json']['descriptionHtml'], '<p>Python and SQL</p>')
        self.assertEqual(posting['raw_json']['employmentType'], 'FULL_TIME')
        self.assertFalse(get.call_args.kwargs['allow_redirects'])
        response.__exit__.assert_called_once()

    def test_rejects_unowned_hosts_boards_credentials_and_traversal_before_get(self):
        for url in ('https://127.0.0.1/job', 'https://jobs.lever.co/other/one',
                    'https://jobs.lever.co.evil.test/alpha/one', 'http://jobs.lever.co/alpha/one',
                    'https://user@jobs.lever.co/alpha/one', 'https://jobs.lever.co:444/alpha/one',
                    'https://jobs.lever.co/alpha/../other', 'https://jobs.lever.co/alpha/%2e%2e/other'):
            with self.subTest(url=url), patch.object(tavily_search.requests, 'get') as get:
                with self.assertRaises(ValueError):
                    tavily_search.verify_posting(url, self.company, 200)
                get.assert_not_called()

    def test_rejects_incomplete_ambiguous_expired_and_invalid_metadata(self):
        variants = [dict(self.record, hiringOrganization={'name': 'Other'}),
                    dict(self.record, description=''), dict(self.record, title=None),
                    dict(self.record, validThrough='2020-01-01T00:00:00Z'),
                    dict(self.record, validThrough='2027-01-01'),
                    dict(self.record, employmentType=['FULL_TIME']),
                    dict(self.record, jobLocation=[None]),
                    dict(self.record, jobLocation={'address': {'addressCountry': 1}}),
                    [self.record, self.record], {}]
        for record in variants:
            with self.subTest(record=record), patch.object(tavily_search.requests, 'get', return_value=self.response(record)):
                with self.assertRaises(ValueError):
                    tavily_search.verify_posting(self.url, self.company, 200)

    def test_rejects_redirect_size_and_timeout_and_closes_response(self):
        for status, chunks in ((302, []), (200, [b'x' * (tavily_search.MAX_RESPONSE_BYTES + 1)])):
            response = self.response(self.record)
            response.status_code, response.iter_content.return_value = status, chunks
            with patch.object(tavily_search.requests, 'get', return_value=response), self.assertRaises(ValueError):
                tavily_search.verify_posting(self.url, self.company, 200)
            response.__exit__.assert_called_once()
        with patch.object(tavily_search.requests, 'get', side_effect=requests.Timeout), self.assertRaises(requests.Timeout):
            tavily_search.verify_posting(self.url, self.company, 200)
        with patch.object(tavily_search.requests, 'get') as get, self.assertRaises(ValueError):
            tavily_search.verify_posting(self.url, self.company, 100)
        get.assert_not_called()

    def test_discovery_skips_existing_duplicates_and_reports_rejected_leads(self):
        urls = [self.url + '/', 'https://evil.test/job', self.url + '/two', self.url + '/two#top']
        with patch.object(tavily_search, 'search', return_value=urls) as search, \
                patch.object(tavily_search.requests, 'get', return_value=self.response(self.record)) as get, \
                self.assertLogs(tavily_search.logger, level='WARNING'):
            additions = tavily_search.discover(self.company, [dict(url=self.url)], 'synthetic', 200)
        self.assertEqual(len(additions), 1)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(search.call_args.args[0], 'site:jobs.lever.co/alpha/ Alpha jobs careers India')
        self.assertNotIn('resume', search.call_args.args[0])
        with patch.object(tavily_search, 'search') as search, self.assertRaises(ValueError):
            tavily_search.discover(self.company, [], 'synthetic', 110)
        search.assert_not_called()

    def test_scope_uses_workday_site_and_registered_ats_board(self):
        self.assertEqual(tavily_search.posting_scope(dict(career_source='slack')),
                         ('salesforce.wd12.myworkdayjobs.com', '/en-US/Slack/job/'))
        self.assertEqual(tavily_search.posting_scope(dict(career_source='notion')),
                         ('jobs.ashbyhq.com', '/notion/'))


class SupplementalWorkerTests(unittest.TestCase):
    def setUp(self):
        test_cloud_worker.CloudEndpointTests.setUp(self)
        self.key_env = patch.dict(os.environ, TAVILY_API_KEY='synthetic')
        self.key_env.start()
        self.addCleanup(self.key_env.stop)

    def call(self, route, data=None):
        response = test_cloud_worker.CloudEndpointTests.call(self, route, data)
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def posting(self):
        return dict(external_id='tavily-synthetic', title='Software Engineer', location='Remote',
                    department=None, url='https://jobs.lever.co/alpha/additional',
                    raw_json=dict(descriptionHtml='Python and SQL', discovery_source='tavily'))

    def test_combined_fetch_observations_enter_existing_groq_queue(self):
        from tests.test_web_discovery import posting
        client = MagicMock()
        client.call.side_effect = self.call
        addition = posting(employer='Alpha')
        with patch.object(runner.pipeline, 'fetch_jobs_for_company', return_value=[]), \
                patch.object(runner.tavily_search, 'search', return_value=[addition['url']]), \
                patch.object(runner.web_discovery, 'verify', side_effect=lambda url, platform, deadline: posting(platform, employer='Alpha')):
            self.assertEqual(runner._fetch_and_score(client, runner.time.monotonic() + 3000, True, None), 0)
        work = self.call('work')['items']
        self.assertEqual(len(work), 1)
        self.assertEqual(work[0]['job']['external_id'], addition['external_id'])
        pool = MagicMock(reporting_failed=False)
        pool.score.return_value = test_evaluation.sample_result()
        with patch.object(runner, 'GroqKeyPool', return_value=pool):
            self.assertEqual(runner.score_pending(client, runner.time.monotonic() + 3000, threading.Event()), 0)
        pool.score.assert_called_once()
        self.assertEqual(self.call('work')['items'], [])
        # The next ATS snapshot must not close or invalidate the independently discovered job.
        company = self.call('companies')['companies'][0]
        self.call('snapshot', dict(company_id=1, fingerprint=company['fingerprint'], jobs=[]))
        self.assertEqual(self.call('work')['items'], [])
        self.assertEqual(self.conn.execute('SELECT is_active FROM jobs WHERE external_id=?', (addition['external_id'],)).fetchone()[0], 1)

    def test_empty_key_disables_discovery_and_failure_preserves_ats(self):
        client = MagicMock()
        client.call.side_effect = self.call
        normal = dict(self.posting(), external_id='native', raw_json={'descriptionHtml': 'Python and SQL'})
        with patch.dict(os.environ, TAVILY_API_KEY=''), \
                patch.object(runner.pipeline, 'fetch_jobs_for_company', return_value=[normal]), \
                patch.object(runner.tavily_search, 'search') as discover:
            self.assertEqual(runner._fetch_and_score(client, runner.time.monotonic() + 3000, True, None), 0)
        discover.assert_not_called()
        with patch.object(runner.pipeline, 'fetch_jobs_for_company', return_value=[normal]), \
                patch.object(runner.tavily_search, 'search', side_effect=requests.Timeout), \
                self.assertLogs(runner.logger, level='ERROR'):
            self.assertEqual(runner._fetch_and_score(client, runner.time.monotonic() + 3000, True, None), 0)
        self.assertEqual(self.conn.execute("SELECT status FROM tavily_searches ORDER BY id DESC LIMIT 1").fetchone()[0], 'failed')
        self.assertEqual(self.conn.execute("SELECT count(*) FROM jobs WHERE external_id='native' AND is_active=1").fetchone()[0], 2)

    def test_storage_prefers_native_and_promotes_discovery_preserving_job_id(self):
        from backend import pipeline_skeleton as pipeline
        addition = self.posting()
        pipeline.upsert_jobs(self.conn, 1, [addition], additive=True)
        job_id = self.conn.execute('SELECT id FROM jobs WHERE external_id=?', (addition['external_id'],)).fetchone()[0]
        native = dict(addition, external_id='native', raw_json={'descriptionHtml': 'Python and SQL'})
        pipeline.upsert_jobs(self.conn, 1, [native])
        self.assertEqual(self.conn.execute("SELECT id FROM jobs WHERE external_id='native'").fetchone()[0], job_id)
        self.assertEqual(pipeline.upsert_jobs(self.conn, 1, [copy.deepcopy(addition)], additive=True), [])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM jobs WHERE url=?', (addition['url'],)).fetchone()[0], 1)
        pipeline.upsert_jobs(self.conn, 1, [])
        self.assertEqual(self.conn.execute('SELECT is_active FROM jobs WHERE id=?', (job_id,)).fetchone()[0], 0)

    def test_discovery_budget_does_not_stop_remaining_ats_fetches(self):
        client = MagicMock()
        client.call.side_effect = self.call
        clock = [100]
        def discovery(client, key, deadline, stop):
            self.assertEqual(deadline, 400)
            clock[0] += 260
            return []
        with patch.object(runner.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(runner.pipeline, 'fetch_jobs_for_company', return_value=[]) as fetch, \
                patch.object(runner, 'discover_roles', side_effect=discovery) as discover:
            self.assertEqual(runner._fetch_and_score(client, 3000, True, None), 0)
        self.assertEqual(fetch.call_count, 2)
        discover.assert_called_once()
