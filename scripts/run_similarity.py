"""Bounded local scoring of hosted work pages; no LLM or Groq requests."""

import logging
import json
import time

from backend import career_sources
from backend.local_embeddings import LocalScorer

logger = logging.getLogger(__name__)
MAX_LOCAL_EVALUATIONS = 1000
MAX_PAGES = 10000


def score_pending(client, deadline, scorer=None):
    owned = scorer is None
    scorer = LocalScorer() if owned else scorer
    try:
        after, job_after, evaluated = 0, 0, 0
        for _ in range(MAX_PAGES):
            if time.monotonic() >= deadline - 30:
                logger.warning("Local similarity time budget reached; remaining work stays pending.")
                return 1
            payload = client.call("similarity/work?after=" + str(after) + "&job_after=" + str(job_after))
            next_user, next_job, items = payload.get("next"), payload.get("job_after"), payload.get("items")
            if (not {"next", "job_after", "items"}.issubset(payload) or not isinstance(items, list) or
                    len(items) > 5 or type(next_job) is not int or not 0 <= next_job <= 2147483647):
                raise ValueError("Invalid similarity work page")
            if next_user is None:
                if items or next_job:
                    raise ValueError("Similarity work lacks a profile cursor")
                logger.info("Local similarity completed: evaluations=%s; no Groq calls made.", evaluated)
                return 0
            if (type(next_user) is not int or not 0 < next_user <= 2147483647 or next_user < after or
                    (job_after and next_user != after) or
                    (next_user == after and (not job_after or next_job != 0 and next_job <= job_after))):
                raise ValueError("Similarity cursor did not advance")
            for item in items:
                if evaluated >= MAX_LOCAL_EVALUATIONS or time.monotonic() >= deadline - 30:
                    logger.warning("Local similarity evaluation budget reached; remaining work stays pending.")
                    return 1
                if (not isinstance(item, dict) or not isinstance(item.get("user"), dict) or
                        item["user"].get("id") != next_user or not isinstance(item.get("job"), dict) or
                        type(item["job"].get("id")) is not int or item["job"]["id"] <= 0 or
                        not isinstance(item.get("fingerprint"), str) or len(item["fingerprint"]) != 64 or
                        not isinstance(item.get("company_name"), str)):
                    raise ValueError("Invalid similarity work item")
                prepared = career_sources.prepare_job(item["job"], deadline - 30)
                if prepared != item["job"]:
                    raw = json.loads(prepared["raw_json"])
                    detail = client.call("description", dict(user_id=next_user, job_id=prepared["id"],
                        fingerprint=item["fingerprint"], description=raw["descriptionHtml"],
                        title=prepared["title"], location=prepared.get("location"), employment_type=raw.get("employmentType")))
                    if (type(detail.get("eligible")) is not bool or not isinstance(detail.get("job"), dict) or
                            detail["job"].get("id") != prepared["id"] or
                            detail["job"].get("company_id") != prepared["company_id"] or
                            not isinstance(detail.get("fingerprint"), str) or len(detail["fingerprint"]) != 64):
                        raise ValueError("Invalid prepared similarity description")
                    if not detail["eligible"]:
                        continue
                    item = dict(item, job=detail["job"], fingerprint=detail["fingerprint"])
                value = scorer.score(item["user"], item["job"], item["company_name"])
                reply = client.call("similarity", dict(user_id=next_user, job_id=item["job"]["id"],
                                    fingerprint=item["fingerprint"], result=value))
                if reply.get("saved") is not True:
                    raise RuntimeError("Hosted similarity result was not acknowledged")
                evaluated += 1
                logger.info("Saved local similarity user_id=%s job_id=%s", next_user, item["job"]["id"])
            after, job_after = next_user, next_job
        logger.warning("Local similarity page budget reached; remaining work stays pending.")
        return 1
    finally:
        if owned:
            scorer.close()
