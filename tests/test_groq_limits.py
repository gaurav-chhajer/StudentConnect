"""Provider header parsing never stores arbitrary header content."""
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from backend import groq_limits
from scripts.run_cloud_worker import GroqKeyPool

class LimitsTests(unittest.TestCase):
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
