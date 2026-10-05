"""
Runs filter -> score for every active user and logs safe IDs and score totals.
Retries rate limits with bounded waits; view evidence in the dashboard.
No email is sent by this command.

Usage:
    python -m scripts.run_local_score

Requires GROQ_API_KEY to be set in your environment, and companies/jobs
already fetched (run run_local_fetch.py first), and at least one user
(run add_test_user.py first).
"""

import logging
import sqlite3
import time
from contextlib import closing

import requests
from backend import pipeline_skeleton as p

logger = logging.getLogger(__name__)
MAX_RATE_LIMIT_RETRIES = 3
MAX_RATE_LIMIT_WAIT_SECONDS = 120


def score_with_rate_limit_retries(user, job, company_name):
    """Retry only rejected requests, never network failures or database writes."""
    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        try:
            return p.score_job_for_user(user, job, company_name)
        except requests.HTTPError as error:
            response = error.response
            if response is None or response.status_code != 429:
                raise
            retry_after = response.headers.get("Retry-After", "")
            if not (isinstance(retry_after, str) and retry_after.isascii()
                    and retry_after.isdigit() and len(retry_after) <= 10):
                raise
            # Respect the server minimum and increase the floor on repeated 429s.
            wait_seconds = max(int(retry_after), 2 ** attempt)
            if attempt == MAX_RATE_LIMIT_RETRIES or wait_seconds > MAX_RATE_LIMIT_WAIT_SECONDS:
                raise
            logger.warning("Groq rate limit: user_id=%s job_id=%s; waiting %s seconds before retry %s/%s.",
                           user["id"], job["id"], wait_seconds, attempt + 1, MAX_RATE_LIMIT_RETRIES)
            remaining = wait_seconds
            while remaining:
                interval = min(remaining, 30)
                time.sleep(interval)
                remaining -= interval


def score_batch(conn):
    """Save valid evaluations; stop external calls when the service is unavailable."""
    users = conn.execute("SELECT * FROM users WHERE active=1").fetchall()
    if not users:
        logger.error("No active users found. Save a profile before scoring.")
        return 1
    failed = False
    for user in users:
        candidates = p.filter_candidate_jobs(conn, user)
        logger.info("Scoring user_id=%s: %s candidate job(s)", user["id"], len(candidates))
        for job in candidates:
            company = conn.execute(
                "SELECT name FROM companies WHERE id=?", (job["company_id"],)
            ).fetchone()
            if company is None:
                raise ValueError("Candidate references a missing company")
            try:
                result = score_with_rate_limit_retries(user, job, company[0])
                p.store_score(conn, job["id"], user["id"], result)
            except requests.HTTPError as error:
                response = error.response
                status = response.status_code if response is not None else None
                if status == 429:
                    retry_after = response.headers.get("Retry-After", "")
                    if isinstance(retry_after, str) and retry_after.isascii() and retry_after.isdigit() and len(retry_after) <= 10:
                        logger.error("Groq rate limit: batch stopped. Retry-After: %s seconds; rerun manually after the limit clears.", retry_after)
                    else:
                        logger.error("Groq rate limit: batch stopped. Check account limits before rerunning manually.")
                else:
                    logger.error("Groq HTTP failure (status=%s): batch stopped; saved scores retained.", status)
                return 1
            except requests.RequestException as error:
                logger.error("Groq network failure (%s): batch stopped; saved scores retained. Rerun manually when connectivity returns.", type(error).__name__)
                return 1
            except ValueError:
                logger.error("Evaluation rejected: user_id=%s job_id=%s; invalid response or input, no score saved.", user["id"], job["id"])
                failed = True
                continue
            logger.info("Saved user_id=%s job_id=%s fit_score=%s", user["id"], job["id"], result["fit_score"])
    return 1 if failed else 0


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        p.init_db()
        with closing(sqlite3.connect(p.DB_PATH, timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            return score_batch(conn)
    except KeyboardInterrupt:
        logger.warning("Scoring interrupted; saved scores retained.")
        return 130
    except (sqlite3.Error, OSError, RuntimeError, ValueError, KeyError, IndexError, TypeError) as error:
        logger.error("Scoring stopped (%s); check configuration and local data. Saved scores retained.", type(error).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
