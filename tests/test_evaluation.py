import copy
import json
import unittest
from unittest.mock import patch

import requests
from backend.evaluation import COMPONENT_LIMITS, validate_evaluation
from backend import pipeline_skeleton as pipeline
from tests import test_web_flows


def sample_result():
    return dict(status="evaluated", components={name: dict(score=maximum, explanation="Synthetic evidence")
                for name, maximum in COMPONENT_LIMITS.items()}, raw_score=100, fit_score=100,
                match_category="Strong Match", reasoning="Relevant synthetic qualifications",
                matched_skills=["Python"], missing_requirements=[], unknown_eligibility=[],
                matched_requirements=[], hard_blockers=[])


class EvaluationTests(unittest.TestCase):
    def test_category_boundaries(self):
        for total, label in ((0,"Not a Match"),(24,"Not a Match"),(25,"Weak Match"),
                             (44,"Weak Match"),(45,"Partial Match"),(64,"Partial Match"),
                             (65,"Good Match"),(79,"Good Match"),(80,"Strong Match"),(100,"Strong Match")):
            result = sample_result()
            remaining = total
            for name, maximum in COMPONENT_LIMITS.items():
                result["components"][name]["score"] = min(remaining, maximum)
                remaining -= result["components"][name]["score"]
            result.update(raw_score=total, fit_score=total, match_category=label)
            self.assertIs(validate_evaluation(result), result)

    def test_blocker_cap_and_unknowns(self):
        result = sample_result()
        result["hard_blockers"] = [dict(requirement="Enrollment", jd_evidence="Must be enrolled",
                                        resume_evidence="Not enrolled")]
        result.update(fit_score=40, match_category="Weak Match")
        validate_evaluation(result, "Not enrolled", "Must be enrolled")
        result["fit_score"] = 100
        with self.assertRaises(ValueError):
            validate_evaluation(result)
        result = sample_result()
        result["unknown_eligibility"] = ["Authorization not stated"]
        validate_evaluation(result)
        self.assertEqual(result["fit_score"],100)

    def test_invalid_outputs_and_invented_quotes(self):
        mutations = [dict(fit_score=True), dict(raw_score=99), dict(match_category="Good Match"),
                     dict(status="unable_to_evaluate"), dict(unknown_eligibility="unknown"),
                     dict(matched_skills=[None]), dict(reasoning=""), dict(components={})]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate_evaluation(dict(sample_result(), **mutation))
        for value in (-1,41,True,12.5):
            result=sample_result()
            result["components"]["skills_match"]["score"]=value
            with self.assertRaises(ValueError):
                validate_evaluation(result)
        result=sample_result()
        result["matched_requirements"]=[dict(requirement="Python",jd_evidence="Python",resume_evidence="Invented")]
        with self.assertRaises(ValueError):
            validate_evaluation(result,"Python project","Python")

    def test_mocked_groq_request_and_failure_paths(self):
        job=dict(title="Intern",department="",location="Remote",raw_json=json.dumps(dict(content="Python")))
        with patch.object(pipeline,"GROQ_API_KEY","test-placeholder"), \
                patch.dict(pipeline.os.environ,{"GROQ_CONTEXT_TOKENS":"131072"}), \
                patch.object(pipeline.requests,"post") as post:
            post.return_value.json.return_value={"choices":[{"message":{"content":json.dumps(sample_result())}}]}
            result=pipeline.score_job_for_user(dict(resume_text="Python project"),job,"Example")
            self.assertEqual(result["match_category"],"Strong Match")
            body=post.call_args.kwargs["json"]
            self.assertEqual(body["messages"][0]["role"],"system")
            self.assertIn("unknown_eligibility",body["messages"][0]["content"])
            self.assertIn("Python project",body["messages"][1]["content"])
            for content in ('{}','not JSON',json.dumps(dict(status="unable_to_evaluate"))):
                post.return_value.json.return_value={"choices":[{"message":{"content":content}}]}
                with self.assertRaises(ValueError):
                    pipeline.score_job_for_user(dict(resume_text="Python"),job,"Example")
            post.side_effect=requests.Timeout()
            with self.assertRaises(requests.Timeout):
                pipeline.score_job_for_user(dict(resume_text="Python"),job,"Example")

    def test_invalid_jd_line_references_are_rejected(self):
        job=dict(title="Python Intern",department="Engineering",location="Remote",
                 raw_json=json.dumps(dict(content="SQL required")))
        for line_id in (None,0,-1,2,True,"1",1.0):
            result=sample_result()
            result["matched_requirements"]=[dict(requirement="Role context",jd_line_id=line_id,
                                                 resume_evidence="Python project")]
            with self.subTest(line_id=line_id),patch.object(pipeline,"GROQ_API_KEY","test-placeholder"), \
                    patch.dict(pipeline.os.environ,{"GROQ_CONTEXT_TOKENS":"131072"}), \
                    patch.object(pipeline.requests,"post") as post:
                post.return_value.json.return_value={"choices":[{"message":{"content":json.dumps(result)}}]}
                with self.assertRaises(ValueError):
                    pipeline.score_job_for_user(dict(resume_text="Python project"),job,"Example")

    def test_exact_jd_line_is_preserved_in_blocker(self):
        line="Minimum 3 years of relevant experience."
        job=dict(title="Engineer",department="",location="Remote",
                 raw_json=json.dumps(dict(content="<p>"+line+"</p>")))
        result=sample_result()
        result.update(fit_score=40,match_category="Weak Match")
        result["hard_blockers"]=[dict(requirement="Professional experience",jd_line_id=1,
                                     resume_line_id=1)]
        with patch.object(pipeline,"GROQ_API_KEY","test-placeholder"), \
                patch.dict(pipeline.os.environ,{"GROQ_CONTEXT_TOKENS":"131072"}), \
                patch.object(pipeline.requests,"post") as post:
            post.return_value.json.return_value={"choices":[{"message":{"content":json.dumps(result)}}]}
            evaluation=pipeline.score_job_for_user(dict(resume_text="No professional experience"),job,"Example")
            self.assertEqual(evaluation["hard_blockers"][0]["jd_evidence"],line)
            self.assertEqual(evaluation["hard_blockers"][0]["resume_evidence"],"No professional experience")
            self.assertIn("Do NOT return jd_evidence",post.call_args.kwargs["json"]["messages"][0]["content"])
            self.assertIn('"jd_line_id": 1',post.call_args.kwargs["json"]["messages"][1]["content"])

    def test_resume_line_references_preserve_text_and_reject_invalid_ids(self):
        job=dict(title="Intern",department="",location="Remote",raw_json=json.dumps(dict(content="Python required")))
        for line_id in (None,0,-1,1,3,True,"2",2.0,2):
            result=sample_result()
            result["matched_requirements"]=[dict(requirement="Python",jd_line_id=1,resume_line_id=line_id)]
            with self.subTest(line_id=line_id), patch.object(pipeline,"GROQ_API_KEY","test-placeholder"), \
                    patch.dict(pipeline.os.environ,{"GROQ_CONTEXT_TOKENS":"131072"}), \
                    patch.object(pipeline.requests,"post") as post:
                post.return_value.json.return_value={"choices":[{"message":{"content":json.dumps(result)}}]}
                if type(line_id) is int and line_id == 2:
                    saved=pipeline.score_job_for_user(dict(resume_text="\n  Python project  \n"),job,"Example")
                    self.assertEqual(saved["matched_requirements"][0]["resume_evidence"],"  Python project  ")
                    self.assertEqual(saved["evidence_format"],"source-line-ids-v1")
                else:
                    with self.assertRaisesRegex(ValueError,"resume_line_id"):
                        pipeline.score_job_for_user(dict(resume_text="\n  Python project  \n"),job,"Example")

    def test_model_cannot_replace_resolved_line_or_invent_resume_evidence(self):
        job=dict(title="Intern",department="",location="Remote",raw_json=json.dumps(dict(content="Python required")))
        entries = [dict(requirement="Python",jd_line_id=1,resume_line_id=1,jd_evidence="Model-written replacement"),
                   dict(requirement="Python",jd_line_id=1,resume_line_id=1,resume_evidence="Invented qualification")]
        for entry in entries:
            result=sample_result()
            result["matched_requirements"]=[entry]
            with self.subTest(entry_type="replacement" if "jd_evidence" in entry else "invented resume"), \
                    patch.object(pipeline,"GROQ_API_KEY","test-placeholder"), \
                    patch.dict(pipeline.os.environ,{"GROQ_CONTEXT_TOKENS":"131072"}), \
                    patch.object(pipeline.requests,"post") as post:
                post.return_value.json.return_value={"choices":[{"message":{"content":json.dumps(result)}}]}
                with self.assertRaises(ValueError):
                    pipeline.score_job_for_user(dict(resume_text="Python project"),job,"Example")

    def test_rejection_identifies_field_without_exposing_evidence(self):
        result=sample_result()
        result["matched_requirements"]=[dict(requirement="Python",jd_evidence="Python",
                                             resume_evidence="PRIVATE invented quote")]
        with self.assertRaisesRegex(ValueError,r"matched_requirements\[0\].resume_evidence") as caught:
            validate_evaluation(result,"Python project","Python")
        self.assertNotIn("PRIVATE",str(caught.exception))


class EvaluationStorageTests(unittest.TestCase):
    setUp = test_web_flows.WebFlowTests.setUp
    seed_user = test_web_flows.WebFlowTests.seed_user
    def test_breakdown_roundtrip_and_safe_rendering(self):
        self.seed_user()
        result=sample_result()
        result["components"]["skills_match"]["explanation"]="<script>unsafe</script>"
        pipeline.store_score(self.conn,1,1,result)
        saved=json.loads(self.conn.execute("SELECT evaluation_json FROM scores WHERE job_id=1").fetchone()[0])
        self.assertEqual(saved["match_category"],"Strong Match")
        page=self.client.get("/jobs?job=1")
        self.addCleanup(page.close)
        self.assertIn(b"Strong Match",page.data)
        self.assertIn(b"40/40",page.data)
        self.assertNotIn(b"<script>unsafe</script>",page.data)
        invalid=copy.deepcopy(result)
        invalid["fit_score"]=50
        with self.assertRaises(ValueError):
            pipeline.store_score(self.conn,1,1,invalid)
        self.assertEqual(self.conn.execute("SELECT fit_score FROM scores WHERE job_id=1").fetchone()[0],100)
