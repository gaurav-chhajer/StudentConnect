"""Daily cloud worker; hosted SQLite stays on PythonAnywhere.

All work is bounded, and uncertain writes/sends are never automatically retried.
No private response bodies or external exception messages are logged.
"""

import argparse
import json
import logging
import os
import time
import uuid

import requests

from backend import hosting, pipeline_skeleton as pipeline
from backend import career_sources
from backend.cloud_worker import MAX_BODY_BYTES, MAX_COMPANIES, MAX_WORK_ITEMS, MAX_JOBS, worker_key
from backend.job_ingestion import deduplicate, mode_for_source, observation_batches

logger = logging.getLogger(__name__)
MATCHING_SECONDS = 50 * 60
FETCH_SECONDS = 20 * 60
EMAIL_SECONDS = 7 * 60
MAX_SCORES = 100
MAX_ACCOUNTS = 1000
CANDIDATE_HTTP_STATUSES = frozenset({400, 413, 422})
GROQ_EVALUATION_RESERVE = 240  # Status reports, one evaluation, hosted save and headroom.


class HostedOperationError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__("Hosted operation failed (HTTP %s)" % status)


class GroqKeyPool:
    """Run-local key selection and credential-free, revisioned status reporting."""
    def __init__(self, client, deadline):
        self.client, self.deadline = client, deadline
        self.keys = (pipeline.GROQ_API_KEY.strip(), os.environ.get("GROQ_API_KEY_2", "").strip(),
                     os.environ.get("GROQ_API_KEY_3", "").strip())
        configured = [key for key in self.keys if key]
        if not self.keys[0]:
            raise ValueError("Configure GROQ_API_KEY before matching")
        if len(configured) != len(set(configured)):
            raise ValueError("Configured Groq key slots must be distinct")
        self.slot = 0
        self.run_id = uuid.uuid4().hex
        self.states = ["not_attempted" if key else "not_configured" for key in self.keys]
        self.revision = 0
        self.reporting_failed = False
        self.publish("running")

    def publish(self, outcome):
        try:
            reply = self.client.call("groq-status", dict(run_id=self.run_id, revision=self.revision,
                                                        states=list(self.states), outcome=outcome))
            if (reply.get("saved") is not True or type(reply.get("revision")) is not int or
                    reply["revision"] != self.revision):
                raise RuntimeError("Hosted key status was not acknowledged")
        except (requests.RequestException, RuntimeError, ValueError, KeyError, TypeError) as error:
            self.reporting_failed = True
            raise RuntimeError("Could not record Groq key status; work stopped") from error
        self.revision += 1

    def state(self, value):
        if self.states[self.slot] != value:
            self.states[self.slot] = value
            self.publish("running")

    def score(self, user, job, company_name):
        for slot in range(self.slot, len(self.keys)):
            if not self.keys[slot]:
                continue
            self.slot = slot
            if time.monotonic() >= self.deadline - GROQ_EVALUATION_RESERVE:
                raise RuntimeError("Matching time budget reached before Groq evaluation")
            if self.states[slot] in ("not_attempted", "failed"):
                self.state("attempting")
            try:
                result = (pipeline.score_job_for_user(user, job, company_name) if slot == 0 else
                          pipeline.score_job_for_user(user, job, company_name, api_key=self.keys[slot]))
            except requests.HTTPError as error:
                limited = error.response is not None and error.response.status_code == 429
                self.state("rate_limited" if limited else "failed")
                if not limited:
                    raise
                logger.warning("Groq Key %s rate limited; recorded in admin history", slot + 1)
                if not any(self.keys[slot + 1:]):
                    raise
                continue
            except (requests.RequestException, ValueError, KeyError, TypeError, RuntimeError):
                self.state("failed")
                raise
            self.state("succeeded")
            return result
        raise RuntimeError("No configured Groq key remains")


class HostedClient:
    def __init__(self):
        self.origin = hosting.site_origin()
        key = worker_key()
        if not self.origin.startswith("https://") or not key:
            raise ValueError("Configure the HTTPS site origin and worker key")
        self.session = requests.Session()
        self.session.headers.update(Authorization="Bearer " + key)

    def close(self):
        self.session.close()

    def call(self, path, data=None):
        encoded = None if data is None else json.dumps(data, allow_nan=False).encode()
        if encoded is not None and len(encoded) > MAX_BODY_BYTES:
            raise ValueError("Hosted request exceeds transfer limit")
        with self.session.request("GET" if data is None else "POST",
                                  self.origin + "/internal/worker/" + path,
                                  data=encoded,
                                  headers={"Content-Type": "application/json"},
                                  timeout=(10, 20) if path == "groq-status" else (10, 90),
                                  allow_redirects=False, stream=True) as response:
            if response.status_code == 409:
                raise ValueError("Hosted inputs changed; work deferred")
            if response.status_code != 200:
                raise HostedOperationError(response.status_code)
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > MAX_BODY_BYTES:
                    raise ValueError("Hosted response exceeds transfer limit")
                chunks.append(chunk)
            payload = json.loads(b"".join(chunks))
            if not isinstance(payload, dict):
                raise ValueError("Hosted response must be an object")
            return payload


def cursor(value, previous):
    if value is None:
        return None
    if type(value) is not int or not previous < value <= 2147483647:
        raise ValueError("Hosted cursor did not advance")
    return value


def fetch_and_score(client, deadline, fetch_only=False):
    if fetch_only:
        return _fetch_and_score(client, deadline, fetch_only=True, pool=None)
    pool = GroqKeyPool(client, deadline)
    result = 1
    try:
        result = _fetch_and_score(client, deadline, fetch_only=False, pool=pool)
        return result
    finally:
        # An uncertain status write must not be retried, including during cleanup.
        if not pool.reporting_failed:
            pool.publish("complete" if result == 0 else "deferred")


def _fetch_and_score(client, deadline, fetch_only, pool):
    if not fetch_only and not pipeline.GROQ_API_KEY.strip():
        raise ValueError("Configure Groq before matching")
    companies = client.call("companies").get("companies")
    if not isinstance(companies, list) or len(companies) > MAX_COMPANIES:
        raise ValueError("Invalid company list")
    successful = set()
    failed = False
    for company in companies:
        if time.monotonic() >= deadline - 120:
            logger.error("Fetching time budget reached; unfinished work waits until the next scheduled run")
            return 1
        stage = "configuration"
        try:
            if (not isinstance(company, dict) or type(company.get("id")) is not int or
                    company.get("ats_type") not in ("greenhouse", "lever", "ashby") or
                    not isinstance(company.get("board_slug"), str) or
                    not company["board_slug"] or len(company["board_slug"]) > 300 or
                    not all(character.isascii() and (character.isalnum() or character in "-_")
                            for character in company["board_slug"])):
                raise ValueError("Invalid board configuration")
            stage = "fetch"
            source = company.get('career_source')
            if source is not None and source not in career_sources.SOURCES:
                raise ValueError('Unknown career source')
            jobs = (career_sources.fetch(source, deadline - 120) if source else
                    pipeline.fetch_jobs_for_company(company))
            stage = "snapshot"
            mode = mode_for_source(source)
            jobs = deduplicate(jobs, mode)
            envelope = dict(company_id=company['id'], fingerprint=company['fingerprint'], mode=mode)
            batches = (observation_batches(envelope, jobs, MAX_JOBS, MAX_BODY_BYTES)
                       if mode == 'observed' else [dict(envelope, jobs=jobs)])
            for batch in batches:
                if time.monotonic() >= deadline:
                    raise ValueError('Observation transfer time budget exhausted')
                client.call('snapshot', batch)
            successful.add(company["id"])
            logger.info("Fetched company_id=%s jobs=%s", company["id"], len(jobs))
        except (requests.RequestException, ValueError, KeyError, TypeError, RuntimeError) as error:
            response = error.response if isinstance(error, requests.RequestException) else None
            company_id = company.get("id") if isinstance(company, dict) else None
            logger.error("Board operation failed: company_id=%s stage=%s error=%s http_status=%s; previous jobs retained if snapshot was not accepted",
                         company_id if type(company_id) is int else "invalid", stage, type(error).__name__,
                         response.status_code if response is not None else "unavailable")
            failed = True
    logger.info("Fetch summary: boards_succeeded=%s boards_total=%s partial_failure=%s",
                len(successful), len(companies), failed)
    if not successful:
        raise RuntimeError("No boards fetched successfully")
    if fetch_only:
        return int(failed)
    after, scores, accounts, skipped = 0, 0, 0, 0
    saved, candidate_failures = 0, 0
    company_query = "".join("&company_id=" + str(company_id) for company_id in sorted(successful))
    for _ in range(MAX_ACCOUNTS):
        if time.monotonic() >= deadline - 120:
            logger.error("Matching time budget reached; remaining scores deferred")
            return 1
        payload = client.call("work?after=" + str(after) + company_query)
        if "next" not in payload:
            raise ValueError("Missing work cursor")
        next_id = cursor(payload["next"], after)
        items = payload.get("items")
        if not isinstance(items, list) or len(items) > MAX_WORK_ITEMS:
            raise ValueError("Invalid work batch")
        if next_id is None:
            if items:
                raise ValueError("Work without an account cursor")
            logger.info("Scoring summary: eligible_accounts=%s evaluations_attempted=%s candidates_skipped_failed_boards=%s scores_saved=%s candidates_failed=%s partial_failure=%s",
                        accounts, scores, skipped, saved, candidate_failures, failed)
            return int(failed)
        after = next_id
        accounts += 1
        for item in items:
            if scores >= MAX_SCORES or time.monotonic() >= deadline - 120:
                logger.error("Scoring budget reached; remaining scores deferred")
                return 1
            if (not isinstance(item, dict) or not isinstance(item.get("user"), dict) or
                    not isinstance(item.get("job"), dict) or item["user"].get("id") != after or
                    type(item["job"].get("id")) is not int or item["job"]["id"] <= 0 or
                    not isinstance(item["user"].get("resume_text"), str) or
                    not isinstance(item.get("company_name"), str) or
                    not isinstance(item.get("fingerprint"), str) or len(item["fingerprint"]) != 64):
                raise ValueError("Invalid scoring work item")
            if item["job"]["company_id"] not in successful:
                skipped += 1
                continue
            scores += 1
            try:
                prepared = career_sources.prepare_job(item['job'], deadline - 120)
            except (requests.RequestException, ValueError, KeyError, TypeError) as error:
                response = error.response if isinstance(error, requests.RequestException) else None
                logger.error('Career description failed: user_id=%s job_id=%s error=%s http_status=%s; candidate deferred',
                             after, item['job']['id'], type(error).__name__,
                             response.status_code if response is not None else 'unavailable')
                failed = True
                candidate_failures += 1
                continue
            stage = "description"
            try:
                if prepared != item['job']:
                    raw = json.loads(prepared['raw_json'])
                    detail = client.call('description', dict(user_id=after, job_id=prepared['id'],
                        fingerprint=item['fingerprint'], description=raw['descriptionHtml'],
                        title=prepared['title'], location=prepared.get('location'), employment_type=raw.get('employmentType')))
                    if type(detail.get('eligible')) is not bool or not isinstance(detail.get('job'), dict):
                        raise ValueError('Invalid description response')
                    if (detail['job'].get('id') != prepared['id'] or
                            detail['job'].get('company_id') != prepared['company_id'] or
                            not isinstance(detail.get('fingerprint'), str) or len(detail['fingerprint']) != 64):
                        raise ValueError('Description response identity changed')
                    if not detail['eligible']:
                        continue
                    item = dict(item, job=detail['job'], fingerprint=detail['fingerprint'])
                # Only an explicit Groq rate-limit rejection permits advancing a key.
                stage = "evaluation"
                result = pool.score(item["user"], item["job"], item["company_name"])
                stage = "save"
                client.call("score", dict(user_id=item["user"]["id"], job_id=item["job"]["id"],
                                          fingerprint=item["fingerprint"], result=result))
                saved += 1
                logger.info("Saved score user_id=%s job_id=%s", item["user"]["id"], item["job"]["id"])
            except (requests.RequestException, ValueError, KeyError, TypeError, RuntimeError) as error:
                response = error.response if isinstance(error, requests.RequestException) else None
                status = (response.status_code if response is not None else
                          error.status if isinstance(error, HostedOperationError) else None)
                # Only known evaluation rejections are local to this candidate.
                # Hosted writes and service availability failures remain stop conditions.
                candidate_failure = stage == "evaluation" and (
                    isinstance(error, (ValueError, KeyError, TypeError)) or
                    (isinstance(error, requests.HTTPError) and status in CANDIDATE_HTTP_STATUSES))
                logger.error("Scoring operation failed: user_id=%s job_id=%s stage=%s error=%s http_status=%s; %s",
                             after, item["job"]["id"], stage, type(error).__name__,
                             status if status is not None else "unavailable",
                             "candidate deferred; continuing" if candidate_failure else "remaining work deferred")
                if candidate_failure:
                    failed = True
                    candidate_failures += 1
                    continue
                return 1
    logger.error("Account budget reached; remaining work deferred")
    return 1


def send_emails(client, deadline):
    after = 0
    for _ in range(MAX_ACCOUNTS):
        if time.monotonic() >= deadline - 100:
            logger.error("Email time budget reached; remaining accounts deferred")
            return 1
        payload = client.call("email", dict(after=after))
        if "next" not in payload or type(payload.get("sent")) is not bool:
            raise ValueError("Invalid email response")
        next_id = cursor(payload["next"], after)
        if next_id is None:
            return 0
        logger.info("Email checked user_id=%s accepted=%s", next_id, payload.get("sent") is True)
        after = next_id
    logger.error("Email account budget reached; remaining accounts deferred")
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("matching", "fetch", "email"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    client = None
    try:
        budgets = {"matching": MATCHING_SECONDS, "fetch": FETCH_SECONDS, "email": EMAIL_SECONDS}
        deadline = time.monotonic() + budgets[args.mode]
        client = HostedClient()
        if args.mode == "email":
            return send_emails(client, deadline)
        return fetch_and_score(client, deadline, fetch_only=args.mode == "fetch")
    except (requests.RequestException, ValueError, RuntimeError, KeyError, TypeError) as error:
        logger.error("Cloud worker stopped (%s); inspect hosted logs before retrying uncertain sends", type(error).__name__)
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
