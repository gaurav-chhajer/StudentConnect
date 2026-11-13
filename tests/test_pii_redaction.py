import json
import unittest
from unittest.mock import patch

from backend import pii_redaction, pipeline_skeleton as pipeline
from tests.test_evaluation import sample_result


class PiiRedactionTests(unittest.TestCase):
    def test_redacts_known_name_email_phone_and_address(self):
        resume = (
            "Asha Kumar\nasha@example.test\n+91 98765 43210\n"
            "Address: 12 Example Road, Bengaluru 560001\n"
            "Experience: built Python APIs"
        )
        cleaned = pii_redaction.redact_resume(resume, "Asha Kumar")
        for value in ("Asha Kumar", "asha@example.test", "98765 43210", "12 Example Road", "560001"):
            self.assertNotIn(value, cleaned)
        self.assertIn("Experience: built Python APIs", cleaned)
        self.assertEqual(cleaned.count("\n"), resume.count("\n"))

    def test_does_not_treat_short_numeric_values_as_phone_numbers(self):
        self.assertEqual(pii_redaction.redact_resume("Graduated in 2024; GPA 3.8"),
                         "Graduated in 2024; GPA 3.8")

    def test_rejects_invalid_inputs(self):
        with self.assertRaises(TypeError):
            pii_redaction.redact_resume(None)
        with self.assertRaises(ValueError):
            pii_redaction.redact_resume("resume", "x" * 201)

    def test_scoring_request_uses_redacted_resume_but_validates_original_evidence(self):
        resume = "Asha Kumar\nasha@example.test\n+91 98765 43210\nPython project"
        job = dict(title="Intern", department="", location="Remote",
                   raw_json=json.dumps({"content": "Python project required"}))
        response = type("Response", (), {
            "headers": {}, "json": lambda self: {"choices": [{"message": {"content": json.dumps(sample_result())}}]},
            "raise_for_status": lambda self: None, "close": lambda self: None,
        })()
        with patch.object(pipeline.requests, "post", return_value=response) as post:
            pipeline.score_job_for_user(dict(name="Asha Kumar", resume_text=resume), job, "Example", api_key="synthetic")
        request_body = post.call_args.kwargs["json"]
        prompt = request_body["messages"][1]["content"]
        self.assertNotIn("Asha Kumar", prompt)
        self.assertNotIn("asha@example.test", prompt)
        self.assertNotIn("98765 43210", prompt)
        self.assertIn("Python project", prompt)


if __name__ == "__main__":
    unittest.main()
