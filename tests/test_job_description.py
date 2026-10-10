import json
import unittest
from unittest.mock import patch

from backend import pipeline_skeleton as pipeline


class JobDescriptionTests(unittest.TestCase):
    def extract(self, raw):
        return pipeline.extract_job_description({"raw_json": json.dumps(raw)})

    def test_greenhouse_decodes_markup_and_keeps_bullets(self):
        self.assertEqual(self.extract({"content":
            "&lt;h2&gt;Requirements&lt;/h2&gt;&lt;ul&gt;&lt;li&gt;Python &amp; SQL"
            "&lt;/li&gt;&lt;/ul&gt;<script>hidden</script>"}),
            "Requirements\n- Python & SQL")

    def test_lever_keeps_requirements_after_long_description(self):
        description = "Introduction. " * 1000
        result = self.extract({
            "descriptionPlain": description,
            "description": "<p>" + description + "</p>",
            "lists": [{"text": "Requirements", "content": "<ul><li>Final requirement</li></ul>"}],
            "additionalPlain": "Application instructions",
        })
        self.assertEqual(result.count(description.strip()), 1)
        self.assertIn("Requirements\n- Final requirement", result)
        self.assertTrue(result.endswith("Application instructions"))

    def test_lever_split_fields_avoid_combined_description_duplicate(self):
        self.assertEqual(self.extract({
            "openingPlain": "Opening", "descriptionBodyPlain": "Body",
            "descriptionPlain": "Opening\nBody", "additionalPlain": "More",
        }), "Opening\n\nBody\n\nMore")

    def test_lever_fetch_removes_only_unused_html_and_preserves_description(self):
        for plain in ("Complete plain requirements", "", "  ", None):
            raw = dict(id="job-1", text="Engineer", categories=dict(commitment="Full-time"),
                       descriptionPlain=plain, description="<p>HTML requirements</p>",
                       openingPlain="Opening", opening="<p>Opening</p>",
                       descriptionBodyPlain="Body", descriptionBody="<p>Body</p>",
                       additionalPlain="Apply here", additional="<p>Apply here</p>",
                       lists=[dict(text="Requirements", content="<ul><li>Final requirement</li></ul>")])
            with self.subTest(plain=plain), patch.object(pipeline.requests, "get") as get:
                get.return_value.json.return_value = [raw]
                fetched = pipeline._fetch_lever("paytm")[0]["raw_json"]
            self.assertEqual(self.extract(fetched), self.extract(raw))
            self.assertEqual(fetched["categories"], raw["categories"])
            self.assertEqual(fetched["lists"], raw["lists"])
            self.assertEqual("description" in fetched, not bool(plain and plain.strip()))
            self.assertNotIn("opening", fetched)
            self.assertNotIn("descriptionBody", fetched)
            self.assertNotIn("additional", fetched)
            self.assertIn("description", raw)  # The source payload is not mutated.

    def test_lever_fetch_keeps_html_only_sections(self):
        raw = dict(id="job-1", text="Engineer", descriptionPlain="",
                   description="<p>Full requirements</p>", additional="<p>Final instructions</p>")
        with patch.object(pipeline.requests, "get") as get:
            get.return_value.json.return_value = [raw]
            fetched = pipeline._fetch_lever("paytm")[0]["raw_json"]
        self.assertEqual(self.extract(fetched), "Full requirements\n\nFinal instructions")

    def test_ashby_prefers_plain_and_falls_back_to_html(self):
        self.assertEqual(self.extract({"descriptionPlain": "Plain", "descriptionHtml": "<p>HTML</p>"}), "Plain")
        self.assertEqual(self.extract({"descriptionHtml": "<h2>Requirements</h2><p>Python</p>"}), "Requirements\nPython")

    def test_missing_and_invalid_descriptions_are_rejected(self):
        for raw in ("{}", "[]", "invalid", '{"content":"<p> </p>"}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                pipeline.extract_job_description({"raw_json": raw})

    def test_scoring_preserves_tail_and_rejects_bad_inputs_before_request(self):
        job = {"title": "Engineer", "department": "Engineering", "location": "Remote",
               "raw_json": json.dumps({"content": "<p>" + "Requirement. " * 1000 + "FINAL REQUIREMENT</p>"})}
        with patch.object(pipeline, "GROQ_API_KEY", "test-placeholder"), \
                patch.object(pipeline, "GROQ_MODEL", "llama-3.3-70b-versatile"), \
                patch.dict(pipeline.os.environ, {"GROQ_CONTEXT_TOKENS": "131072"}), \
                patch.object(pipeline.requests, "post") as post:
            from tests.test_evaluation import sample_result
            post.return_value.json.return_value = {"choices": [{"message": {"content": json.dumps(sample_result())}}]}
            pipeline.score_job_for_user({"resume_text": "Python"}, job, "Example")
            prompt = post.call_args.kwargs["json"]["messages"][1]["content"]
            self.assertIn("FINAL REQUIREMENT", prompt)
            self.assertIn("Location: Remote", prompt)
            self.assertNotIn("<p>", prompt)
            post.reset_mock()
            for raw in ("{}", json.dumps({"content": "x" * 140000})):
                with self.subTest(raw_length=len(raw)), self.assertRaises(ValueError):
                    pipeline.score_job_for_user({"resume_text": "Python"}, dict(job, raw_json=raw), "Example")
            post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
