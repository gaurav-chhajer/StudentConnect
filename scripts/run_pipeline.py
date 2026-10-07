"""One scheduled cycle: fetch, score verified accounts, send due digests.

Default previews saved eligible digests without network calls. --run performs
real ATS/Groq/Gmail operations. The scheduler must use the website's database.
"""

import argparse
import logging
import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

import requests

from backend import pipeline_skeleton as pipeline
from scripts import run_local_email, run_local_fetch, run_local_score

logger = logging.getLogger(__name__)
DEFAULT_MAX_SCORES = 20
MAX_SCORES_PER_CYCLE = 100
MAX_SCORES_PER_ACCOUNT = 5


@contextmanager
def exclusive_cycle(path):
    """OS lock is released on exit/crash; the lock file itself is retained."""
    with Path(path).open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise RuntimeError("Another pipeline cycle is active or locking failed") from error
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise RuntimeError("Another pipeline cycle is active or locking failed") from error
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def fetch_cycle(conn):
    """Failed snapshots leave previous jobs intact; successful empty ones close jobs."""
    run_local_fetch.seed_companies(conn)
    successful = set()
    failed = False
    for company in conn.execute("SELECT * FROM companies WHERE active=1 ORDER BY id").fetchall():
        try:
            jobs = pipeline.fetch_jobs_for_company(company)
        except (requests.RequestException, ValueError, KeyError, TypeError) as error:
            logger.error("Fetch failed: company_id=%s error=%s; previous jobs retained",
                         company["id"], type(error).__name__)
            failed = True
            continue
        # Database failures propagate: never continue after a partial write.
        pipeline.upsert_jobs(conn, company["id"], jobs)
        if not jobs:
            with conn:
                conn.execute("UPDATE jobs SET is_active=0 WHERE company_id=?", (company["id"],))
        successful.add(company["id"])
        logger.info("Fetched company_id=%s jobs=%s", company["id"], len(jobs))
    return successful, failed


def score_cycle(conn, company_ids, max_scores):
    """Bound requests and prioritize accounts least recently scored."""
    users = conn.execute("""SELECT u.* FROM users u JOIN accounts a ON a.user_id=u.id
        WHERE u.active=1 AND a.email_verified=1 AND trim(u.resume_text)<>''
        ORDER BY COALESCE((SELECT MAX(scored_at) FROM scores WHERE user_id=u.id),''),u.id""").fetchall()
    names = {row["id"]: row["name"] for row in conn.execute("SELECT id,name FROM companies")}
    attempted = 0
    failed = False
    for user in users:
        candidates = pipeline.filter_candidate_jobs(conn, user)
        # A small share per account prevents one resume from using the full cycle.
        for job in [job for job in candidates if job["company_id"] in company_ids][:MAX_SCORES_PER_ACCOUNT]:
            if attempted >= max_scores:
                logger.info("Scoring budget reached; remaining candidates wait for the next cycle.")
                return int(failed)
            attempted += 1
            try:
                result = run_local_score.score_with_rate_limit_retries(user, job, names[job["company_id"]])
            except requests.RequestException as error:
                logger.error("Scoring service stopped (%s); valid saved scores retained", type(error).__name__)
                return 1
            except ValueError:
                logger.error("Evaluation rejected: user_id=%s job_id=%s; no score saved", user["id"], job["id"])
                failed = True
                continue
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute("""SELECT u.resume_text,u.preferences_json FROM users u
                    JOIN accounts a ON a.user_id=u.id WHERE u.id=? AND u.active=1 AND a.email_verified=1""",
                    (user["id"],)).fetchone()
                posting = conn.execute("""SELECT j.raw_json FROM jobs j JOIN companies c ON c.id=j.company_id
                    WHERE j.id=? AND j.is_active=1 AND c.active=1""", (job["id"],)).fetchone()
                subscribed = conn.execute("""SELECT NOT EXISTS(SELECT 1 FROM user_settings
                    WHERE user_id=? AND subscriptions_configured=1) OR EXISTS(SELECT 1 FROM company_subscriptions
                    WHERE user_id=? AND company_id=?)""", (user["id"], user["id"], job["company_id"])).fetchone()[0]
                if (current is None or posting is None or not subscribed or
                        current["resume_text"] != user["resume_text"] or
                        current["preferences_json"] != user["preferences_json"] or posting[0] != job["raw_json"]):
                    logger.warning("Inputs changed during evaluation: user_id=%s job_id=%s; result discarded",
                                   user["id"], job["id"])
                    continue
                pipeline.store_score(conn, job["id"], user["id"], result)
            logger.info("Saved user_id=%s job_id=%s fit_score=%s", user["id"], job["id"], result["fit_score"])
    return int(failed)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Fetch, score and send real scheduled digests")
    parser.add_argument("--max-scores", type=int, default=DEFAULT_MAX_SCORES, help="Job evaluations per cycle (1-100; default 20; existing 429 retries apply)")
    args = parser.parse_args(argv)
    if not 1 <= args.max_scores <= MAX_SCORES_PER_CYCLE:
        parser.error("--max-scores must be between 1 and 100")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    database = Path(pipeline.DB_PATH)
    pause = database.parent / "email_schedule.paused"
    try:
        if args.run:
            if pause.exists():
                logger.error("Scheduled email is paused; review Gmail and sent records before resuming.")
                return 1
            if not pipeline.GROQ_API_KEY.strip():
                logger.error("Set GROQ_API_KEY in the worker environment before enabling automation.")
                return 1
            pipeline.validate_email_configuration()
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=rw", uri=True, timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            accounts = conn.execute("""SELECT COUNT(*) FROM accounts a JOIN users u ON u.id=a.user_id
                WHERE u.active=1 AND a.email_verified=1""").fetchone()[0]
            if not accounts:
                logger.info("No verified active accounts; pipeline skipped.")
                return 0
            if not args.run:
                run_local_email.send_digest(conn, scheduled=True)
                logger.info("Preview only: no jobs fetched, no Groq calls, no emails sent.")
                return 0
            with exclusive_cycle(database.parent / "pipeline.lock"):
                if pause.exists():
                    raise RuntimeError("Scheduled mail became paused")
                companies, fetch_failed = fetch_cycle(conn)
                if not companies:
                    logger.error("No boards fetched successfully; scoring and email skipped.")
                    return 1
                score_status = score_cycle(conn, companies, args.max_scores)
                # Only validated, current saved scores from successful boards can
                # be emailed, even when another evaluation failed this cycle.
                with pause.open("x", encoding="utf-8") as marker:
                    marker.write("Pipeline delivery in progress or requires provider review.\n")
                run_local_email.send_digest(conn, send=True, scheduled=True, company_ids=companies)
                pause.unlink()
                logger.info("Pipeline cycle finished; fetch_failed=%s score_status=%s", fetch_failed, score_status)
                return 1 if fetch_failed or score_status else 0
    except KeyboardInterrupt:
        logger.warning("Pipeline interrupted; saved results retained. Check any email pause marker.")
        return 130
    except (requests.RequestException, sqlite3.Error, OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
        logger.error("Pipeline stopped (%s); check configuration, services and any email pause marker",
                     type(error).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
