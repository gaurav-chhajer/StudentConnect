"""Offline coverage for broad search, privacy, persistent quotas and queue ingestion."""

import json
import sqlite3
import threading
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import requests

from backend import cloud_worker, role_titles, tavily_search, web_discovery
from scripts import run_cloud_worker as runner, run_pipeline
from tests import test_cloud_worker, test_evaluation

NOW = datetime(2026, 11, 10, 6, tzinfo=timezone.utc)


def posting(platform='LinkedIn', employer='Gamma', title='Software Engineer', description='Python and SQL'):
    url = 'https://' + web_discovery.PLATFORMS[platform] + '/jobs/one'
    record = dict(title=title, description=description, hiringOrganization={'name': employer})
    value = tavily_search.posting_from_record(record, url, employer)
    value['employer'] = employer
    value['raw_json']['discovery_platform'] = platform
    return value


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        test_cloud_worker.CloudEndpointTests.setUp(self)
        clock = patch.object(web_discovery, 'datetime', wraps=datetime)
        self.clock = clock.start()
        self.clock.now.return_value = NOW
        self.addCleanup(clock.stop)

    def call(self, route, data=None):
        return test_cloud_worker.CloudEndpointTests.call(self, route, data)

    def reserve(self):
        response = self.call('discovery/reserve', {})
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def result(self, reservation, postings=None, status='complete', rejected=0):
        return self.call('discovery/results', dict(id=reservation['search']['id'], status=status,
            postings=postings or [], rejected=rejected))

    def selected(self):
        self.conn.execute('INSERT INTO user_settings(user_id,subscriptions_configured) VALUES(1,1)')
        self.conn.commit()

    def test_role_only_reservations_rotate_all_platforms_then_titles(self):
        searches = [self.reserve()['search'] for _ in range(8)]
        self.assertEqual([s['platform'] for s in searches[:7]], list(web_discovery.PLATFORMS))
        self.assertEqual({s['title'] for s in searches[:7]}, {'Software Engineer'})
        self.assertEqual(searches[7]['title'], 'Backend Developer')
        self.assertEqual(set(searches[0]), {'id', 'title', 'platform', 'domain'})
        self.assertNotIn('Python resume', json.dumps(searches))
        self.assertEqual(self.conn.execute('SELECT daily_used,monthly_used FROM tavily_budget').fetchone()[:], (8, 8))

    def test_selected_companies_do_not_trigger_broad_discovery(self):
        self.selected()
        self.assertEqual(self.reserve()['reason'], 'no_ready_role_titles')
        self.assertEqual(self.conn.execute('SELECT daily_used FROM tavily_budget').fetchone()[0], 0)

    def test_missing_profile_is_requested_even_with_no_existing_jobs(self):
        self.conn.execute('DELETE FROM resume_role_titles')
        self.conn.execute('UPDATE jobs SET is_active=0')
        self.conn.commit()
        self.assertEqual(self.reserve()['reason'], 'no_ready_role_titles')
        self.assertIn('role_profile_required', self.call('work').json)
        self.selected()
        self.assertNotIn('role_profile_required', self.call('work').json)

    def test_daily_budget_survives_new_connections_and_resets_at_ist_midnight(self):
        for _ in range(33):
            self.assertIsNotNone(self.reserve()['search'])
        self.client = self.app.test_client()
        self.assertEqual(self.reserve()['reason'], 'budget_exhausted')
        with closing(sqlite3.connect(self.database)) as fresh:
            self.assertEqual(fresh.execute('SELECT daily_used FROM tavily_budget').fetchone()[0], 33)
        self.clock.now.return_value = datetime(2026, 11, 10, 18, 29, tzinfo=timezone.utc)
        self.assertIsNone(self.reserve()['search'])
        self.clock.now.return_value += timedelta(minutes=1)
        next_day = self.reserve()
        self.assertEqual(next_day['daily_used'], 1)
        self.assertEqual(next_day['monthly_used'], 34)

    def test_monthly_cap_prevents_31_day_overspend_and_resets_next_month(self):
        self.reserve()
        self.conn.execute("UPDATE tavily_budget SET day='2026-11-09',monthly_used=999")
        self.conn.commit()
        self.assertEqual(self.reserve()['monthly_used'], 1000)
        self.assertEqual(self.reserve()['reason'], 'budget_exhausted')
        self.clock.now.return_value = datetime(2026, 12, 1, 0, tzinfo=timezone.utc)
        self.assertEqual(self.reserve()['monthly_used'], 1)

    def test_competing_workers_cannot_spend_the_last_daily_credit_twice(self):
        self.reserve()
        self.conn.execute('UPDATE tavily_budget SET daily_used=32,monthly_used=32')
        self.conn.commit()
        barrier = threading.Barrier(2)

        def claim():
            with closing(sqlite3.connect(self.database, timeout=5)) as conn:
                conn.row_factory = sqlite3.Row
                barrier.wait(timeout=5)
                with conn:
                    conn.execute('BEGIN IMMEDIATE')
                    return web_discovery.reserve(conn, NOW)

        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = [executor.submit(claim) for _ in range(2)]
            results = [future.result(timeout=10) for future in claims]
        self.assertEqual(sum(result['search'] is not None for result in results), 1)
        self.assertEqual(self.conn.execute('SELECT daily_used FROM tavily_budget').fetchone()[0], 33)

    def test_round_robin_between_eligible_accounts(self):
        self.conn.execute("INSERT INTO users(id,email,resume_text) VALUES(2,'other@example.test','Python resume')")
        self.conn.execute("INSERT INTO accounts(user_id,password_hash,created_at) VALUES(2,'fixture',?)", (NOW.isoformat(),))
        role_titles.save(self.conn, dict(id=2, resume_text='Python resume'), test_cloud_worker.title_profile())
        self.conn.commit()
        for _ in range(4):
            self.reserve()
        self.assertEqual([row[0] for row in self.conn.execute('SELECT user_id FROM tavily_searches ORDER BY id')], [1, 2, 1, 2])

    def test_verified_new_employer_enters_queue_and_can_be_scored(self):
        reservation = self.reserve()
        response = self.result(reservation, [posting()])
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['saved'], 1)
        company = self.conn.execute("SELECT * FROM companies WHERE name='Gamma'").fetchone()
        self.assertEqual(company['discovery_only'], 1)
        self.assertNotIn(company['id'], [row['id'] for row in self.call('companies').json['companies']])
        self.conn.execute('UPDATE jobs SET is_active=0 WHERE company_id IN (1,2)')
        self.conn.commit()
        self.assertEqual(len(self.call('work').json['items']), 1)
        client = MagicMock()
        client.call.side_effect = lambda path, data=None: self.call(path, data).json
        pool = MagicMock(reporting_failed=False)
        pool.score.return_value = test_evaluation.sample_result()
        with patch.object(runner, 'GroqKeyPool', return_value=pool):
            self.assertEqual(runner.score_pending(client, runner.time.monotonic() + 3000, threading.Event()), 0)
        pool.score.assert_called_once()
        self.assertEqual(self.call('work').json['items'], [])
        with patch.object(run_pipeline.pipeline, 'fetch_jobs_for_company', return_value=[]) as fetch:
            successful, failed = run_pipeline.fetch_cycle(self.conn, {company['id']})
        fetch.assert_not_called()
        self.assertFalse(failed)
        self.assertEqual(successful, {company['id']})
        self.assertEqual(self.conn.execute('SELECT is_active FROM jobs WHERE company_id=?', (company['id'],)).fetchone()[0], 1)

    def test_duplicate_platform_postings_and_replayed_results_are_idempotent(self):
        first = self.reserve()
        self.assertEqual(self.result(first, [posting()]).json['saved'], 1)
        self.assertTrue(self.result(first, [posting()]).json['already_completed'])
        second = self.reserve()
        self.assertEqual(second['search']['platform'], 'Indeed')
        self.assertEqual(self.result(second, [posting('Indeed')]).json['saved'], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM jobs WHERE external_id LIKE 'tavily-%'").fetchone()[0], 1)

    def test_scope_or_resume_change_discards_results_without_refunding_credit(self):
        for change in ('scope', 'resume'):
            with self.subTest(change=change):
                reservation = self.reserve()
                value = posting(reservation['search']['platform'])
                if change == 'scope':
                    self.selected()
                else:
                    self.conn.execute("UPDATE users SET resume_text='Changed resume' WHERE id=1")
                    self.conn.commit()
                response = self.result(reservation, [value])
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json['inputs_changed'])
                self.conn.execute('DELETE FROM user_settings')
                self.conn.commit()
        self.assertEqual(self.conn.execute('SELECT daily_used FROM tavily_budget').fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM companies WHERE name='Gamma'").fetchone()[0], 0)

    def test_invalid_postings_roll_back_whole_batch_and_do_not_create_employers(self):
        reservation = self.reserve()
        bad = posting()
        bad['url'] = 'https://evil.test/job'
        response = self.result(reservation, [posting(), bad])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM companies WHERE name='Gamma'").fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT status FROM tavily_searches').fetchone()[0], 'reserved')

    def test_discovery_requires_worker_auth_and_rejects_bad_counters(self):
        self.assertEqual(self.client.post('/internal/worker/discovery/reserve', json={}).status_code, 401)
        reservation = self.reserve()
        for rejected in (-1, True, 11):
            self.assertEqual(self.result(reservation, rejected=rejected).status_code, 400)
        self.assertEqual(self.result(reservation, [posting()], status='failed').status_code, 400)
        self.assertEqual(self.call('discovery/reserve', {'title': 'injected'}).status_code, 400)

    def test_selected_user_cannot_score_discovery_from_an_unselected_employer(self):
        self.result(self.reserve(), [posting()])
        self.selected()
        self.conn.execute('INSERT INTO company_subscriptions(user_id,company_id,subscribed_at) VALUES(1,1,?)', (NOW.isoformat(),))
        self.conn.commit()
        self.assertEqual({item['company_name'] for item in self.call('work').json['items']}, {'Alpha'})


class VerificationTests(unittest.TestCase):
    def test_rejects_external_hosts_credentials_traversal_and_redirects(self):
        for url in ('http://linkedin.com/jobs/1', 'https://linkedin.com.evil.test/jobs/1',
                    'https://user@linkedin.com/jobs/1', 'https://linkedin.com:444/jobs/1',
                    'https://linkedin.com/a/../b', 'https://linkedin.com/%2e%2e/job'):
            with self.subTest(url=url), patch.object(tavily_search.requests, 'get') as get:
                with self.assertRaises(ValueError):
                    web_discovery.verify(url, 'LinkedIn', 1000)
                get.assert_not_called()
        response = MagicMock(status_code=302)
        response.__enter__.return_value = response
        with patch.object(tavily_search.requests, 'get', return_value=response), self.assertRaises(ValueError):
            web_discovery.verify('https://linkedin.com/jobs/1', 'LinkedIn', runner.time.monotonic() + 30)
        response.__exit__.assert_called_once()

    def test_verifies_each_platform_and_preserves_complete_description(self):
        record = dict(title='Software Engineer', description='<p>Python and SQL; full tail.</p>',
                      hiringOrganization={'name': 'Gamma'})
        record['@type'] = 'JobPosting'
        for platform, domain in web_discovery.PLATFORMS.items():
            response = MagicMock(status_code=200)
            response.__enter__.return_value = response
            response.iter_content.return_value = [('<script type="application/ld+json">' + json.dumps(record) + '</script>').encode()]
            with self.subTest(platform=platform), patch.object(tavily_search.requests, 'get', return_value=response) as get:
                value = web_discovery.verify('https://' + domain + '/jobs/1', platform, runner.time.monotonic() + 30)
                web_discovery.validate_posting(value, platform)
                self.assertIn('full tail.', value['raw_json']['descriptionHtml'])
                self.assertFalse(get.call_args.kwargs['allow_redirects'])

    def test_missing_structured_posting_employer_or_expired_job_is_rejected(self):
        for record in ({}, dict(title='Engineer', description='Python', hiringOrganization={}),
                       dict(title='Engineer', description='Python', hiringOrganization={'name': 'Gamma'},
                            validThrough='2020-01-01T00:00:00Z')):
            with patch.object(tavily_search, 'read_record', return_value=record), self.assertRaises(ValueError):
                web_discovery.verify('https://linkedin.com/jobs/1', 'LinkedIn', 1000)


class WorkerSearchTests(unittest.TestCase):
    def reservation(self):
        return dict(search=dict(id=1, title='Software Engineer', platform='LinkedIn', domain='linkedin.com'))

    def test_search_body_contains_only_role_query_and_platform_filter(self):
        client = MagicMock()
        client.call.side_effect = [self.reservation(), dict(saved=0), dict(search=None, reason='budget_exhausted')]
        response = MagicMock(status_code=200)
        response.__enter__.return_value = response
        response.iter_content.return_value = [b'{"results":[]}']
        with patch.object(tavily_search.requests, 'post', return_value=response) as post:
            runner.discover_roles(client, 'synthetic', runner.time.monotonic() + 100)
        payload = post.call_args.kwargs['json']
        self.assertEqual(payload, dict(query='Software Engineer', search_depth='basic', max_results=10,
                                      auto_parameters=False, include_domains=['linkedin.com']))
        self.assertEqual(post.call_count, 1)

    def test_provider_timeout_is_recorded_once_without_retry_or_remaining_budget_burn(self):
        client = MagicMock()
        client.call.side_effect = [self.reservation(), dict(saved=0)]
        with patch.object(tavily_search, 'search', side_effect=requests.Timeout) as search, self.assertLogs(runner.logger, level='ERROR'):
            runner.discover_roles(client, 'synthetic', runner.time.monotonic() + 100)
        search.assert_called_once()
        self.assertEqual(client.call.call_args.args[1]['status'], 'failed')
        self.assertEqual(client.call.call_count, 2)

    def test_unverified_postings_are_counted_and_uncertain_hosted_writes_propagate(self):
        client = MagicMock()
        client.call.side_effect = [self.reservation(), requests.Timeout()]
        with patch.object(tavily_search, 'search', return_value=['https://linkedin.com/jobs/1']), \
                patch.object(web_discovery, 'verify', side_effect=ValueError), \
                self.assertLogs(runner.logger, level='WARNING'), self.assertRaises(requests.Timeout):
            runner.discover_roles(client, 'synthetic', runner.time.monotonic() + 100)
        self.assertEqual(client.call.call_args.args[1]['rejected'], 1)

    def test_shutdown_and_expired_budget_do_not_reserve_credits(self):
        client = MagicMock()
        stop = threading.Event()
        stop.set()
        runner.discover_roles(client, 'synthetic', runner.time.monotonic() + 100, stop)
        runner.discover_roles(client, 'synthetic', runner.time.monotonic())
        client.call.assert_not_called()
