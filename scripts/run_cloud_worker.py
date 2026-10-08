"""Daily cloud worker; hosted SQLite stays on PythonAnywhere.

All work is bounded, and uncertain writes/sends are never automatically retried.
No private response bodies or external exception messages are logged.
"""

import argparse
import json
import logging
import time

import requests

from backend import hosting, pipeline_skeleton as pipeline
from backend.cloud_worker import MAX_BODY_BYTES, MAX_COMPANIES, MAX_WORK_ITEMS, worker_key

logger = logging.getLogger(__name__)
MATCHING_SECONDS = 35 * 60
FETCH_SECONDS = 7 * 60
EMAIL_SECONDS = 7 * 60
MAX_SCORES = 100
MAX_ACCOUNTS = 1000


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
                                  timeout=(10, 90), allow_redirects=False, stream=True) as response:
            if response.status_code == 409:
                raise ValueError("Hosted inputs changed; work deferred")
            if response.status_code != 200:
                raise RuntimeError("Hosted operation failed (HTTP %s)" % response.status_code)
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
        try:
            if (not isinstance(company, dict) or type(company.get("id")) is not int or
                    company.get("ats_type") not in ("greenhouse", "lever", "ashby") or
                    not isinstance(company.get("board_slug"), str) or
                    not company["board_slug"] or len(company["board_slug"]) > 300 or
                    not all(character.isascii() and (character.isalnum() or character in "-_")
                            for character in company["board_slug"])):
                raise ValueError("Invalid board configuration")
            jobs = pipeline.fetch_jobs_for_company(company)
            client.call("snapshot", dict(company_id=company["id"], fingerprint=company["fingerprint"], jobs=jobs))
            successful.add(company["id"])
            logger.info("Fetched company_id=%s jobs=%s", company["id"], len(jobs))
        except (requests.RequestException, ValueError, KeyError, TypeError, RuntimeError) as error:
            logger.error("Board operation failed (%s); previous jobs retained if snapshot was not accepted", type(error).__name__)
            failed = True
    if not successful:
        raise RuntimeError("No boards fetched successfully")
    if fetch_only:
        return int(failed)
    after, scores = 0, 0
    for _ in range(MAX_ACCOUNTS):
        if time.monotonic() >= deadline - 120:
            logger.error("Matching time budget reached; remaining scores deferred")
            return 1
        payload = client.call("work?after=" + str(after))
        if "next" not in payload:
            raise ValueError("Missing work cursor")
        next_id = cursor(payload["next"], after)
        items = payload.get("items")
        if not isinstance(items, list) or len(items) > MAX_WORK_ITEMS:
            raise ValueError("Invalid work batch")
        if next_id is None:
            if items:
                raise ValueError("Work without an account cursor")
            return int(failed)
        after = next_id
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
                continue
            scores += 1
            try:
                # One bounded request; a rejected/failed evaluation waits for the next daily run.
                result = pipeline.score_job_for_user(item["user"], item["job"], item["company_name"])
                client.call("score", dict(user_id=item["user"]["id"], job_id=item["job"]["id"],
                                          fingerprint=item["fingerprint"], result=result))
                logger.info("Saved score user_id=%s job_id=%s", item["user"]["id"], item["job"]["id"])
            except (requests.RequestException, ValueError, KeyError, TypeError, RuntimeError) as error:
                logger.error("Scoring operation failed (%s); remaining work deferred", type(error).__name__)
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
