"""Authenticated, bounded operations for the scheduled cloud worker.

The hosted database remains authoritative. No uploaded files, credentials,
password hashes or account tokens are returned to the worker.
"""

import hashlib
import hmac
import json
import os
import re
import smtplib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Blueprint, current_app, request

from backend import matching_tasks, pipeline_skeleton as pipeline, store
from backend.evaluation import validate_evaluation
from scripts import run_pipeline

MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_COMPANIES = 1000
MAX_JOBS = 5000
MAX_WORK_ITEMS = 5
INDIA = timezone(timedelta(hours=5, minutes=30))


def worker_key():
    value = os.environ.get("STUDENTCONNECT_WORKER_KEY", "")
    if value and not re.fullmatch(r"[a-fA-F0-9]{64}", value):
        raise ValueError("Worker key must contain 64 hexadecimal characters")
    return value


def authenticate():
    """Separate bearer authentication; browser sessions cannot authorize work."""
    key = worker_key()
    supplied = request.headers.get("Authorization", "")
    return bool(key and not request.headers.get("Cookie") and
                hmac.compare_digest(supplied.encode(), ("Bearer " + key).encode()))


def fingerprint(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(worker_key().encode(), encoded, hashlib.sha256).hexdigest()


def positive_id(value):
    if type(value) is not int or not 1 <= value <= 2147483647:
        raise ValueError("Invalid identifier")
    return value


def body():
    if request.content_length is None or request.content_length > MAX_BODY_BYTES:
        raise ValueError("Worker request exceeds its size limit")
    value = request.get_json()
    if not isinstance(value, dict):
        raise ValueError("Worker request must be an object")
    return value


def eligible_user(conn, user_id):
    return conn.execute("""SELECT u.id,u.resume_text,u.preferences_json FROM users u
        JOIN accounts a ON a.user_id=u.id WHERE u.id=? AND u.active=1
        AND a.email_verified=1 AND trim(u.resume_text)<>''""", (user_id,)).fetchone()


def work_identity(user, job):
    return dict(user=dict(user), job=dict(job))


def fixed_schedule_due(conn, user_id, now):
    """The cloud schedule is 08:00 India time; preserve cadence and opt-outs."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Cloud schedule requires a timezone-aware clock")
    cadence = store.settings(conn, user_id)["cadence"]
    if cadence == "off":
        return False
    if cadence not in ("daily", "weekly"):
        raise ValueError("Invalid cadence")
    local = now.astimezone(INDIA)
    if local.hour < 8:
        return False
    last = conn.execute("SELECT MAX(sent_at) FROM sent_log WHERE user_id=? AND channel='email'",
                        (user_id,)).fetchone()[0]
    if not last:
        return True
    previous = datetime.fromisoformat(last)
    if previous.tzinfo is None or previous.utcoffset() is None:
        raise ValueError("Saved send timestamp must include a timezone")
    return (local.date() - previous.astimezone(INDIA).date()).days >= (7 if cadence == "weekly" else 1)


def register_routes(application, connection, database):
    worker_key()  # Invalid configured credentials must fail at startup.
    routes = Blueprint("worker", __name__, url_prefix="/internal/worker")

    @routes.errorhandler(ValueError)
    def invalid(error):
        current_app.logger.warning("Cloud worker rejected invalid input")
        return {"error": "Invalid worker input"}, 400

    @routes.errorhandler(RuntimeError)
    def unavailable(error):
        current_app.logger.error("Cloud worker operation unavailable")
        return {"error": "Worker operation unavailable; inspect hosted logs"}, 503

    @routes.get("/companies")
    def companies():
        rows = connection().execute("""SELECT id,name,ats_type,board_slug FROM companies
            WHERE active=1 ORDER BY id LIMIT ?""", (MAX_COMPANIES + 1,)).fetchall()
        if len(rows) > MAX_COMPANIES:
            raise ValueError("Too many companies for one daily cycle")
        return {"companies": [dict(row, fingerprint=fingerprint(dict(row))) for row in rows]}

    @routes.post("/snapshot")
    def snapshot():
        data = body()
        company_id = positive_id(data.get("company_id"))
        jobs = data.get("jobs")
        if not isinstance(jobs, list) or len(jobs) > MAX_JOBS:
            raise ValueError("Invalid snapshot")
        seen = set()
        for job in jobs:
            if not isinstance(job, dict):
                raise ValueError("Invalid job")
            for field, maximum in (("external_id", 300), ("title", 1000)):
                value = job.get(field)
                if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                    raise ValueError("Invalid job text")
            for field in ("location", "department", "url"):
                value = job.get(field)
                if value is not None and (not isinstance(value, str) or len(value) > 4000):
                    raise ValueError("Invalid optional job text")
            if job.get("url") and store.safe_url(job["url"]) is None:
                raise ValueError("Invalid application URL")
            if not isinstance(job.get("raw_json"), dict) or job["external_id"] in seen:
                raise ValueError("Invalid or duplicate job")
            seen.add(job["external_id"])
        conn = connection()
        with run_pipeline.exclusive_cycle(Path(database).parent / "pipeline.lock"), conn:
            conn.execute("BEGIN IMMEDIATE")
            company = conn.execute("SELECT id,name,ats_type,board_slug FROM companies WHERE id=? AND active=1",
                                   (company_id,)).fetchone()
            if company is None or data.get("fingerprint") != fingerprint(dict(company)):
                return {"error": "Company changed; fetch again"}, 409
            pipeline.upsert_jobs(conn, company_id, jobs)
        return {"saved": len(jobs)}

    @routes.get("/work")
    def work():
        after = store.integer(request.args.get("after", "0"), 0, 2147483647)
        conn = connection()
        user = conn.execute("""SELECT u.id,u.resume_text,u.preferences_json FROM users u
            JOIN accounts a ON a.user_id=u.id WHERE u.id>? AND u.active=1
            AND a.email_verified=1 AND trim(u.resume_text)<>'' ORDER BY u.id LIMIT 1""", (after,)).fetchone()
        if user is None:
            return {"items": [], "next": None}
        items = []
        for job in pipeline.filter_candidate_jobs(conn, user)[:MAX_WORK_ITEMS]:
            company = conn.execute("SELECT name FROM companies WHERE id=?", (job["company_id"],)).fetchone()
            items.append(dict(user=dict(user), job=dict(job), company_name=company[0],
                              fingerprint=fingerprint(work_identity(user, job))))
        payload = {"items": items, "next": user["id"]}
        if len(json.dumps(payload).encode()) > MAX_BODY_BYTES:
            raise ValueError("Work batch exceeds transfer limit")
        return payload

    @routes.post("/score")
    def score():
        data = body()
        user_id, job_id = positive_id(data.get("user_id")), positive_id(data.get("job_id"))
        conn = connection()
        with run_pipeline.exclusive_cycle(Path(database).parent / "pipeline.lock"), conn:
            conn.execute("BEGIN IMMEDIATE")
            user = eligible_user(conn, user_id)
            job = conn.execute("SELECT * FROM jobs WHERE id=? AND is_active=1", (job_id,)).fetchone()
            if user is None or job is None or data.get("fingerprint") != fingerprint(work_identity(user, job)):
                return {"error": "Inputs changed; result discarded"}, 409
            if job["company_id"] not in matching_tasks.companies_for_user(conn, user_id):
                return {"error": "Subscription changed; result discarded"}, 409
            existing = conn.execute("SELECT is_stale FROM scores WHERE user_id=? AND job_id=?", (user_id, job_id)).fetchone()
            if existing and not existing[0]:
                return {"saved": False, "already_scored": True}
            result = data.get("result")
            validate_evaluation(result, user["resume_text"], pipeline.extract_job_description(job))
            task = conn.execute("SELECT revision FROM matching_tasks WHERE user_id=?", (user_id,)).fetchone()
            pipeline.store_score(conn, job_id, user_id, result)
            if task and not pipeline.filter_candidate_jobs(conn, user):
                conn.execute("""UPDATE matching_tasks SET status='complete',last_error=NULL
                    WHERE user_id=? AND revision=?""", (user_id, task[0]))
        return {"saved": True}

    @routes.post("/email")
    def email():
        after = body().get("after", 0)
        if type(after) is not int or not 0 <= after <= 2147483647:
            raise ValueError("Invalid email cursor")
        conn = connection()
        pause = Path(database).parent / "email_schedule.paused"
        with run_pipeline.exclusive_cycle(Path(database).parent / "pipeline.lock"), conn:
            if pause.exists():
                raise RuntimeError("Email delivery paused; reconcile Gmail and sent_log")
            conn.execute("BEGIN IMMEDIATE")
            user = conn.execute("""SELECT u.* FROM users u JOIN accounts a ON a.user_id=u.id
                WHERE u.id>? AND u.active=1 AND a.email_verified=1 ORDER BY u.id LIMIT 1""", (after,)).fetchone()
            if user is None:
                return {"next": None, "sent": False}
            now = datetime.now(timezone.utc)
            start = now.astimezone(INDIA).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
            refreshed = {row[0] for row in conn.execute("""SELECT DISTINCT company_id FROM jobs
                WHERE is_active=1 AND last_seen_at>=? AND last_seen_at<=?""",
                (start.isoformat(), now.isoformat()))}
            matches = pipeline.get_unsent_top_matches(conn, user["id"], company_ids=refreshed)
            if not fixed_schedule_due(conn, user["id"], now) or not matches:
                return {"next": user["id"], "sent": False}
            pipeline.validate_email_configuration()
            with pause.open("x", encoding="utf-8") as marker:
                marker.write("Cloud email attempt requires review unless completed.\n")
            try:
                pipeline.deliver_email(user, matches)
                pipeline.mark_sent(conn, user["id"], [match["job_id"] for match in matches])
                pause.unlink()
            except (smtplib.SMTPException, OSError, ValueError, RuntimeError):
                current_app.logger.error("Cloud digest failed or is uncertain; delivery paused (user_id=%s)", user["id"])
                return {"error": "Delivery uncertain; inspect Gmail and hosted send records"}, 503
            current_app.logger.info("Gmail accepted cloud digest (user_id=%s matches=%s)", user["id"], len(matches))
            return {"next": user["id"], "sent": True}

    application.register_blueprint(routes)
