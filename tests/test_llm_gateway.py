import json
import sqlite3
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from backend import deterministic_store, llm_gateway


class LlmGatewayTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        deterministic_store.initialize(self.conn)
        self.addCleanup(self.conn.close)
        self.result = {"score": 62, "components": {"must_have": 0.8}, "matched": [{"skill": "Python"}],
                       "missing_must_have_requirements": [{"jd": "GraphQL", "jd_line": 2}],
                       "resume_text": "PRIVATE RESUME MUST NEVER BE SENT"}

    def provider(self, invoke, *, rpm=10, requests_per_day=10, tokens_per_day=10000):
        return llm_gateway.Provider("fake", "small", "large", rpm, requests_per_day,
                                    tokens_per_day, invoke)

    def test_valid_explanation_uses_large_model_for_borderline_without_changing_score(self):
        calls = []

        def invoke(model, prompt):
            calls.append((model, prompt))
            return json.dumps({"explanation": "Evidence supports partial alignment.",
                               "gap_summary": "GraphQL is missing."}), 20, 10

        gateway = llm_gateway.Gateway([self.provider(invoke)])
        output = gateway.explain(self.conn, self.result, user_id=1)
        self.assertEqual(calls[0][0], "large")
        self.assertNotIn("PRIVATE RESUME", calls[0][1])
        self.assertEqual(output["explanation_source"], "llm")
        self.assertEqual(self.result["score"], 62)
        self.assertEqual(self.conn.execute("SELECT status FROM deterministic_llm_calls").fetchone()[0], "succeeded")

    def test_invalid_json_retries_once_then_uses_template(self):
        calls = []

        def invoke(model, prompt):
            calls.append(model)
            return "not json", None, None

        gateway = llm_gateway.Gateway([self.provider(invoke)], sleep=lambda _: None, jitter=lambda: 0)
        output = gateway.explain(self.conn, self.result, user_id=1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(output["explanation_source"], "template")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM deterministic_llm_calls").fetchone()[0], 2)

    def test_daily_request_limit_skips_provider_and_returns_template(self):
        def invoke(model, prompt):
            self.fail("Daily-limited provider must be skipped")

        provider = self.provider(invoke, requests_per_day=1)
        self.conn.execute("""INSERT INTO deterministic_llm_calls
            (provider,model,latency_ms,status,created_at) VALUES('fake','small',0,'failed',?)""",
            (datetime.now(timezone.utc).isoformat(),))
        self.conn.commit()
        gateway = llm_gateway.Gateway([provider])
        output = gateway.explain(self.conn, self.result, user_id=1)
        self.assertEqual(output["explanation_source"], "template")

    def test_per_user_daily_limit_is_three_and_isolated_between_users(self):
        calls = []

        def invoke(model, prompt):
            calls.append(model)
            return json.dumps({"explanation": "Evidence supports partial alignment.",
                               "gap_summary": "Review missing skills."}), 20, 10

        now = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
        gateway = llm_gateway.Gateway([self.provider(invoke)], wall_clock=lambda: now)
        for _ in range(3):
            self.assertEqual(gateway.explain(self.conn, self.result, user_id=1)["explanation_source"], "llm")
        self.assertEqual(gateway.explain(self.conn, self.result, user_id=1)["explanation_source"], "template")
        self.assertEqual(gateway.explain(self.conn, self.result, user_id=2)["explanation_source"], "llm")
        self.assertEqual(len(calls), 4)
        rows = self.conn.execute("SELECT user_id,COUNT(*) FROM deterministic_user_llm_requests GROUP BY user_id")
        self.assertEqual(dict(rows.fetchall()), {1: 3, 2: 1})

    def test_llm_explanation_requires_a_positive_user_id_when_enabled(self):
        gateway = llm_gateway.Gateway([self.provider(lambda *_: self.fail("Invalid IDs must fail before invocation"))])
        with self.assertRaises(ValueError):
            gateway.explain(self.conn, self.result, user_id=0)

    def test_invalid_provider_configuration_is_rejected(self):
        with self.assertRaises(ValueError):
            llm_gateway.Provider("fake", "small", "large", 0, 2, 3, lambda *_: None)

    def test_configured_providers_require_explicit_models_and_budgets(self):
        with patch.dict("os.environ", {"GROQ_API_KEY": "synthetic", "GROQ_MODEL": "small"}, clear=True):
            self.assertEqual(llm_gateway.configured_providers(), [])
        configured = {"GROQ_API_KEY": "synthetic", "GROQ_MODEL": "small", "GROQ_LARGE_MODEL": "large",
                      "LLM_GROQ_RPM": "10", "LLM_GROQ_DAILY_REQUESTS": "20",
                      "LLM_GROQ_DAILY_TOKENS": "20000"}
        with patch.dict("os.environ", configured, clear=True):
            providers = llm_gateway.configured_providers()
        self.assertEqual([provider.name for provider in providers], ["groq"])


if __name__ == "__main__":
    unittest.main()
