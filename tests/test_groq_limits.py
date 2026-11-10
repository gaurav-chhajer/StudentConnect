"""Provider header parsing never stores arbitrary header content."""
import unittest
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
import requests
from backend import groq_limits
from scripts import run_cloud_worker as runner
from scripts.run_cloud_worker import GroqKeyPool
from tests import test_cloud_worker

class LimitsTests(unittest.TestCase):
    def rejection(self, message, retry='120', status=429):
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps({'error': {'message': message}}).encode()
        if retry is not None:
            response.headers['retry-after'] = retry
        return response

    def test_rejection_reports_specific_quota_without_private_message(self):
        now = datetime(2026, 11, 9, tzinfo=timezone.utc)
        response = self.rejection('private org_private output tokens per minute (OTPM): Limit 1000, Requested 1174')
        with patch.object(groq_limits, 'datetime', wraps=datetime) as dates:
            dates.now.return_value = now
            metrics = groq_limits.rejection(response)
        self.assertEqual(metrics['limited_dimension'], 'output_tokens_per_minute')
        self.assertEqual(metrics['rejected_limit'], 1000)
        self.assertEqual(metrics['rejected_requested'], 1174)
        self.assertEqual(metrics['request_fit'], 'oversized')
        self.assertEqual(metrics['retry_at'], (now + timedelta(seconds=120)).isoformat())
        self.assertNotIn('private', json.dumps(metrics))
        groq_limits.validate_metrics(metrics)

    def test_malformed_or_large_error_body_is_explicitly_unavailable(self):
        for content in (b'not JSON', b'x' * 40000, b'[]'):
            response = self.rejection('unused')
            response._content = content
            self.assertEqual(groq_limits.rejection(response)['rejection_detail'], 'unavailable')

    def test_unbounded_numeric_metadata_and_invalid_retry_are_ignored(self):
        metrics = groq_limits.rejection(self.rejection('Limit ' + '9' * 5000, retry='private'))
        self.assertNotIn('rejected_limit', metrics)
        self.assertNotIn('retry_at', metrics)
        groq_limits.validate_metrics(metrics)

    def test_http_date_retry_and_message_duration(self):
        now = datetime(2026, 11, 9, tzinfo=timezone.utc)
        with patch.object(groq_limits, 'datetime', wraps=datetime) as dates:
            dates.now.return_value = now
            metrics = groq_limits.capture({'retry-after': 'Mon, 09 Nov 2026 00:02:00 GMT'})
            self.assertEqual(metrics['retry_at'], (now + timedelta(seconds=120)).isoformat())
            metrics = groq_limits.rejection(self.rejection('Please try again in 1m2s.', retry=None))
            self.assertEqual(metrics['retry_at'], (now + timedelta(seconds=62)).isoformat())

    def test_quota_state_prevents_provider_calls_in_a_new_batch(self):
        now = datetime(2026, 11, 9, tzinfo=timezone.utc)
        response = self.rejection('tokens per minute (TPM): Limit 8000, Used 7900, Requested 1000')
        client = MagicMock()
        client.call.return_value = {'saved': True}
        with patch.object(groq_limits, 'datetime', wraps=datetime) as dates, \
                patch.object(groq_limits.time, 'monotonic', return_value=100), \
                patch.object(runner.pipeline, 'GROQ_API_KEY', 'synthetic'), \
                patch.dict(runner.os.environ, GROQ_API_KEY_2='', GROQ_API_KEY_3='',
                           GROQ_API_KEY_4='', GROQ_API_KEY_5='', GROQ_API_KEY_6='', GROQ_API_KEY_7=''), \
                patch.object(GroqKeyPool, 'publish'):
            dates.now.return_value = now
            quota = groq_limits.QuotaState()
            first = GroqKeyPool(client, 3000, quota=quota)
            with self.assertRaises(groq_limits.CooldownPending) as pending:
                first._request(MagicMock(side_effect=requests.HTTPError(response=response)))
            self.assertEqual(pending.exception.seconds, 120)
            second = GroqKeyPool(client, 3000, quota=quota)
            provider = MagicMock()
            with self.assertRaises(groq_limits.CooldownPending):
                second._request(provider)
            provider.assert_not_called()
            dates.now.return_value = now + timedelta(seconds=121)
            with patch.object(groq_limits.time, 'monotonic', return_value=221):
                self.assertEqual(second._request(lambda key: 'resumed'), 'resumed')

    def test_available_key_can_run_while_another_key_is_cooling(self):
        now = datetime(2026, 11, 9, tzinfo=timezone.utc)
        with patch.object(groq_limits, 'datetime', wraps=datetime) as dates, \
                patch.object(groq_limits.time, 'monotonic', return_value=100), \
                patch.object(runner.pipeline, 'GROQ_API_KEY', 'synthetic-one'), \
                patch.dict(runner.os.environ, GROQ_API_KEY_2='synthetic-two', GROQ_API_KEY_3='',
                           GROQ_API_KEY_4='', GROQ_API_KEY_5='', GROQ_API_KEY_6='', GROQ_API_KEY_7=''), \
                patch.object(GroqKeyPool, 'publish'):
            dates.now.return_value = now
            quota = groq_limits.QuotaState()
            quota.record(0, {'retry_at': (now + timedelta(seconds=120)).isoformat()})
            pool = GroqKeyPool(MagicMock(), 3000, quota=quota)
            provider = MagicMock(return_value='done')
            self.assertEqual(pool._request(provider), 'done')
            provider.assert_called_once_with('synthetic-two')

    def test_duration_and_count_headers(self):
        result=groq_limits.capture({'x-ratelimit-limit-requests':'1000','x-ratelimit-reset-requests':'2m59.56s','x-ratelimit-reset-tokens':'5ms','Authorization':'private'})
        self.assertEqual(result['limit_requests'],1000)
        self.assertLess(abs((datetime.fromisoformat(result['reset_requests'])-datetime.now(timezone.utc)).total_seconds()-179.56),2)
        self.assertEqual(set(result),{'limit_requests','reset_requests','reset_tokens'})

    def test_invalid_missing_and_unbounded_headers(self):
        self.assertEqual(groq_limits.capture({}),{})
        self.assertEqual(groq_limits.capture({'x-ratelimit-limit-tokens':'-1','x-ratelimit-reset-tokens':'999999999d','x-ratelimit-reset-requests':'secret'}),{})

    def test_worker_reports_only_allowlisted_metrics(self):
        from unittest.mock import MagicMock
        pool=object.__new__(GroqKeyPool)
        pool.slot=1
        pool.client=MagicMock()
        pool.client.call.return_value={'saved':True}
        pool.observe_limits({'x-ratelimit-remaining-tokens':'42','Authorization':'private'})
        args=pool.client.call.call_args.args
        self.assertEqual(args[0],'groq-limits')
        self.assertEqual(args[1]['key_slot'],2)
        self.assertEqual(args[1]['metrics'],{'remaining_tokens':42})
        pool.client.call.return_value={'saved':False}
        with self.assertRaises(RuntimeError):
            pool.observe_limits({'x-ratelimit-remaining-tokens':'42'})


class QuotaEndpointTests(unittest.TestCase):
    setUp = test_cloud_worker.CloudEndpointTests.setUp
    call = test_cloud_worker.CloudEndpointTests.call

    def test_rejection_survives_header_refresh_and_invalid_values_are_rejected(self):
        metrics = dict(limited_dimension='output_tokens_per_minute', rejected_limit=1000,
                       rejected_requested=1174, request_fit='oversized',
                       rejection_detail='parsed', rejected_at='2026-11-09T00:00:00+00:00',
                       retry_at='2026-11-09T00:02:00+00:00')
        payload = dict(key_slot=1, model='fixture', metrics=metrics)
        self.assertEqual(self.call('groq-limits', payload).status_code, 200)
        self.assertEqual(self.call('groq-limits', dict(payload, metrics={'remaining_tokens': 8000})).status_code, 200)
        saved = json.loads(self.conn.execute('SELECT metrics_json FROM groq_limits WHERE key_slot=1').fetchone()[0])
        self.assertEqual(saved['rejected_requested'], 1174)
        self.assertEqual(saved['remaining_tokens'], 8000)
        for invalid in ({'limited_dimension': 'private raw message'}, {'rejected_limit': True},
                        {'retry_at': '2026-11-09'}, {'rejected_requested': 1e50},
                        {'rejected_requested': 10**500}):
            with self.subTest(invalid=invalid):
                self.assertEqual(self.call('groq-limits', dict(payload, metrics=invalid)).status_code, 400)
