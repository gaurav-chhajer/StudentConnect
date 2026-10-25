"""Hosted authorization, result validity, local worker pagination and provider selection."""

import json
import unittest
from unittest.mock import MagicMock, patch

from backend import cloud_worker, local_embeddings, pipeline_skeleton as pipeline, similarity
from scripts import run_cloud_worker as runner, run_similarity, watch_hosted_jobs as watcher
from tests import test_cloud_worker


class SimilarityEndpointTests(unittest.TestCase):
    setUp = test_cloud_worker.CloudEndpointTests.setUp
    call = test_cloud_worker.CloudEndpointTests.call

    def value(self, item):
        return dict(user_id=item["user"]["id"], job_id=item["job"]["id"], fingerprint=item["fingerprint"],
                    result=similarity.result(0.7, item["user"]["resume_text"], pipeline.extract_job_description(item["job"])))

    def test_scores_remain_separate_and_current_results_are_reused(self):
        before = [dict(row) for row in self.conn.execute("SELECT * FROM scores")]
        item = self.call("similarity/work").json["items"][0]
        self.assertEqual(self.call("similarity", self.value(item)).json, {"saved": True})
        self.assertNotIn(item["job"]["id"], [value["job"]["id"] for value in self.call("similarity/work").json["items"]])
        self.assertEqual(before, [dict(row) for row in self.conn.execute("SELECT * FROM scores")])
        matches, _ = similarity.matches(self.conn, 1, 1)
        self.assertEqual(len(matches), 1)
        self.conn.execute("UPDATE users SET resume_text='Changed resume' WHERE id=1")
        self.conn.commit()
        self.assertEqual(similarity.matches(self.conn, 1, 1)[0], [])
        self.assertEqual(self.call("similarity", self.value(item)).status_code, 409)
        self.assertIn(item["job"]["id"], [value["job"]["id"] for value in self.call("similarity/work").json["items"]])

    def test_bad_results_stale_jobs_and_unsubscribed_writes_fail(self):
        item = self.call("similarity/work").json["items"][0]
        value = self.value(item)
        for mutation in ({"cosine": True}, {"matched_skills": ["Invented"]}, {"rank_score": 99}, {"model_revision": "old"}):
            bad = dict(value, result=dict(value["result"], **mutation))
            self.assertEqual(self.call("similarity", bad).status_code, 400)
        self.conn.execute("UPDATE jobs SET title='Changed intern' WHERE id=?", (item["job"]["id"],))
        self.conn.commit()
        self.assertEqual(self.call("similarity", value).status_code, 409)
        item = self.call("similarity/work").json["items"][0]
        self.conn.execute("INSERT INTO user_settings(user_id,subscriptions_configured) VALUES(1,1)")
        self.conn.commit()
        self.assertEqual(self.call("similarity", self.value(item)).status_code, 409)

    def test_all_jobs_are_paged_without_repeating_five_job_shortlist(self):
        original = self.conn.execute("SELECT * FROM jobs WHERE id=1").fetchone()
        for number in range(3, 15):
            self.conn.execute("""INSERT INTO jobs(company_id,external_id,title,url,raw_json,first_seen_at,last_seen_at)
                VALUES(1,?,?,?,?,?,?)""", (str(number), "Python Intern", original["url"], original["raw_json"], original["first_seen_at"], original["last_seen_at"]))
        self.conn.commit()
        after, job_after, seen = 0, 0, []
        for _ in range(10):
            page = self.call("similarity/work?after=" + str(after) + "&job_after=" + str(job_after)).json
            if page["next"] is None:
                break
            seen.extend(item["job"]["id"] for item in page["items"])
            after, job_after = page["next"], page["job_after"]
        self.assertEqual(seen, list(range(1, 15)))
        self.assertEqual(self.client.get("/internal/worker/similarity/work").status_code, 401)
        self.assertEqual(self.call("similarity/work?after=-1").status_code, 400)

    def test_private_view_hides_other_users_and_changed_jobs(self):
        item = self.call("similarity/work").json["items"][0]
        self.call("similarity", self.value(item))
        self.assertEqual(self.client.get("/jobs/local").status_code, 302)
        with self.client.session_transaction() as session:
            session["user_id"], session["session_version"] = 1, 0
        page = self.client.get("/jobs/local")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Relevance index", page.data)
        self.assertNotIn(b"Strong Match", page.data)
        self.assertEqual(similarity.matches(self.conn, 2, 1)[0], [])
        self.conn.execute("UPDATE jobs SET raw_json=? WHERE id=?", (json.dumps({"content": "New SQL description"}), item["job"]["id"]))
        self.conn.commit()
        self.assertEqual(similarity.matches(self.conn, 1, 1)[0], [])

    def test_local_runner_saves_without_groq_or_status_calls(self):
        class Client:
            def __init__(inner):
                inner.paths = []
            def call(inner, path, data=None):
                inner.paths.append(path)
                response = self.call(path, data)
                if response.status_code != 200:
                    raise RuntimeError("Synthetic hosted rejection")
                return response.json
        client = Client()
        scorer = MagicMock()
        scorer.score.side_effect = lambda user, job, company: similarity.result(0.7, user["resume_text"], pipeline.extract_job_description(job))
        with patch.object(pipeline, "score_job_for_user") as groq, patch.object(run_similarity.time, "monotonic", return_value=0):
            self.assertEqual(run_similarity.score_pending(client, 1000, scorer), 0)
            self.assertEqual(run_similarity.score_pending(client, 1000, scorer), 0)
        self.assertEqual(scorer.score.call_count, 2)
        groq.assert_not_called()
        self.assertFalse(any("groq" in path for path in client.paths))


class SimilaritySelectionTests(unittest.TestCase):
    def test_default_watcher_is_local_and_never_sends_fit_alerts(self):
        client = MagicMock()
        with patch.object(runner, "HostedClient", return_value=client), \
                patch.object(runner, "fetch_and_score", return_value=0) as score, \
                patch.object(watcher.time, "monotonic", return_value=0), patch.object(runner, "send_emails") as email:
            self.assertEqual(watcher.run_cycle(), 0)
        score.assert_called_once_with(client, runner.MATCHING_SECONDS, fetch_only=False, provider="similarity")
        email.assert_not_called()
        client.close.assert_called_once()

    def test_local_provider_does_not_construct_groq_pool_and_closes_scorer(self):
        with patch.object(local_embeddings, "LocalScorer") as scorer, \
                patch.object(runner, "_fetch_and_score", return_value=0), \
                patch.object(run_similarity, "score_pending", return_value=0) as pending, \
                patch.object(runner, "GroqKeyPool") as groq:
            self.assertEqual(runner.fetch_and_score(MagicMock(), 1000, provider="similarity"), 0)
        groq.assert_not_called()
        pending.assert_called_once()
        scorer.return_value.close.assert_called_once()

    def test_cursor_failures_and_budget_do_not_retry_or_fallback(self):
        client, scorer = MagicMock(), MagicMock()
        client.call.return_value = dict(items=[], next=1, job_after=5)
        with patch.object(run_similarity.time, "monotonic", return_value=0):
            with self.assertRaises(ValueError):
                run_similarity.score_pending(client, 1000, scorer)
        self.assertEqual(client.call.call_count, 2)
        client.reset_mock()
        with patch.object(run_similarity.time, "monotonic", return_value=999):
            self.assertEqual(run_similarity.score_pending(client, 1000, scorer), 1)
        client.call.assert_not_called()
