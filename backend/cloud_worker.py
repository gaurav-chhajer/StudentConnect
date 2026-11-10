"""Authenticated, bounded operations for the scheduled cloud worker.

The hosted database remains authoritative. No uploaded files, credentials,
password hashes or account tokens are returned to the worker.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import smtplib
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Blueprint, current_app, request

from backend import match_reminders, matching_tasks, pipeline_skeleton as pipeline, role_titles, similarity, store
from backend.evaluation import validate_evaluation
from backend import groq_limits as groq_limits_module
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


@contextmanager
def _groq_status_transaction(connection, database, run_id, revision, started):
    """Time status persistence, including commit on early/idempotent returns."""
    logger = current_app.logger.getChild('groq_status')
    stage = 'connection'

    def checkpoint(value):
        logger.info(
            'Groq status timing: run_id=%s revision=%s stage=%s elapsed_seconds=%.3f',
            run_id, revision, value, time.monotonic() - started)
    try:
        checkpoint(stage)
        conn = connection()
        stage = 'lock_wait'
        checkpoint(stage)
        with run_pipeline.exclusive_cycle(Path(database).parent / 'pipeline.lock'):
            checkpoint('lock_acquired')
            with conn:
                stage = 'begin_wait'
                checkpoint(stage)
                conn.execute('BEGIN IMMEDIATE')
                stage = 'save'
                checkpoint('transaction_started')
                yield conn
                stage = 'commit'
                checkpoint('save_complete')
            checkpoint('commit_complete')
        checkpoint('lock_released')
    except Exception as error:
        # Observe and re-raise; existing request boundaries still own the response.
        logger.error(
            'Groq status failed: run_id=%s revision=%s stage=%s error=%s elapsed_seconds=%.3f',
            run_id, revision, stage, type(error).__name__, time.monotonic() - started)
        raise


def register_routes(application, connection, database):
    worker_key()  # Invalid configured credentials must fail at startup.
    # Keep diagnostic checkpoints visible without changing other application log levels.
    application.logger.getChild('groq_status').setLevel(logging.INFO)
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
        rows = connection().execute("""SELECT id,name,ats_type,board_slug,career_source FROM companies
            WHERE active=1 AND discovery_only=0 ORDER BY id LIMIT ?""", (MAX_COMPANIES + 1,)).fetchall()
        if len(rows) > MAX_COMPANIES:
            raise ValueError("Too many companies for one daily cycle")
        return {"companies": [dict(row, fingerprint=fingerprint(dict(row))) for row in rows]}

    @routes.post('/sync-catalog')
    def sync_catalog():
        from backend.career_sources import SOURCES, UNVERIFIED_SOURCES, VERIFIED_WORLDWIDE_SOURCES
        sources = body().get('sources')
        if (not isinstance(sources, list) or len(sources) > MAX_COMPANIES or
                any(not isinstance(key, str) or key not in SOURCES or
                    key in UNVERIFIED_SOURCES or SOURCES[key][1] == 'blocked' for key in sources)):
            raise ValueError('Invalid verified career sources')
        conn = connection()
        with run_pipeline.exclusive_cycle(Path(database).parent / 'pipeline.lock'), conn:
            conn.execute('BEGIN IMMEDIATE')
            for key in sources:
                if conn.execute('SELECT 1 FROM companies WHERE career_source=?', (key,)).fetchone():
                    if key in VERIFIED_WORLDWIDE_SOURCES:
                        conn.execute('UPDATE companies SET active=1 WHERE career_source=? AND active=0', (key,))
                    continue
                conn.execute('''INSERT INTO companies(name,ats_type,board_slug,career_source,active)
                    VALUES(?,'greenhouse',?,?,1)''', (SOURCES[key][0], 'career-site-' + key, key))
        return {'saved': True}

    @routes.post('/groq-limits')
    def groq_limits():
        from backend.groq_limits import REJECTION_FIELDS, validate_metrics
        data=body()
        if set(data)!={'key_slot','model','metrics'}:
            raise ValueError('Invalid limit fields')
        slot,model,metrics=data['key_slot'],data['model'],data['metrics']
        if type(slot) is not int or not 1<=slot<=groq_limits_module.KEY_SLOT_COUNT or not isinstance(model,str) or not 1<=len(model)<=200:
            raise ValueError('Invalid limit identity')
        validate_metrics(metrics)
        with connection() as conn:
            previous = conn.execute('SELECT model,metrics_json FROM groq_limits WHERE key_slot=?', (slot,)).fetchone()
            if previous and previous['model'] == model and 'rejected_at' not in metrics:
                retained = json.loads(previous['metrics_json'])
                metrics = dict({field: retained[field] for field in REJECTION_FIELDS if field in retained}, **metrics)
            conn.execute("""INSERT INTO groq_limits(key_slot,model,metrics_json,updated_at) VALUES(?,?,?,?) ON CONFLICT(key_slot) DO UPDATE SET model=excluded.model,metrics_json=excluded.metrics_json,updated_at=excluded.updated_at""",
                (slot,model,json.dumps(metrics),datetime.now(timezone.utc).isoformat()))
        return {'saved':True}

    @routes.post("/groq-status")
    def groq_status():
        started = time.monotonic()
        current_app.logger.getChild('groq_status').info('Groq status handler entered')
        data = body()
        if set(data) != {"run_id", "revision", "states", "job_counts", "outcome"}:
            raise ValueError("Invalid key history fields")
        run_id, revision = data["run_id"], data["revision"]
        states, job_counts, outcome = data["states"], data["job_counts"], data["outcome"]
        allowed = {"not_configured", "not_attempted", "attempting", "succeeded", "rate_limited", "failed"}
        if (not isinstance(run_id, str) or re.fullmatch(r"[a-f0-9]{32}", run_id) is None or
                type(revision) is not int or not 0 <= revision <= 1000 or
                not isinstance(states, list) or len(states) not in (3, 5, groq_limits_module.KEY_SLOT_COUNT) or
                any(not isinstance(state, str) or state not in allowed for state in states) or
                not isinstance(job_counts, list) or len(job_counts) != len(states) or
                any(type(count) is not int or not 0 <= count <= 1000000 for count in job_counts) or
                not isinstance(outcome, str) or outcome not in {"running", "complete", "deferred"}):
            raise ValueError("Invalid key history")
        with _groq_status_transaction(connection, database, run_id, revision, started) as conn:
            previous = conn.execute("SELECT * FROM groq_key_history WHERE run_id=? ORDER BY key_slot",
                                    (run_id,)).fetchall()
            reported_at = datetime.now(timezone.utc)
            now = reported_at.isoformat()
            if not previous:
                if (revision != 0 or outcome != "running" or states[0] != "not_attempted" or
                        any(state not in {"not_configured", "not_attempted"} for state in states)):
                    raise ValueError("History must begin with unattempted configured keys")
                conn.executemany("""INSERT INTO groq_key_history
                    (run_id,key_slot,state,outcome,revision,started_at,updated_at,job_count) VALUES(?,?,?,'running',0,?,?,?)""",
                    [(run_id, slot, state, now, now, job_counts[slot - 1]) for slot, state in enumerate(states, 1)])
                increments = job_counts
            else:
                if len(previous) != len(states):
                    raise ValueError("Key slot count cannot change within a run")
                old_revision = previous[0]["revision"]
                if revision == old_revision:
                    if (states != [row["state"] for row in previous] or
                            job_counts != [row["job_count"] for row in previous] or
                            outcome != previous[0]["outcome"]):
                        return {"error": "History revision already recorded"}, 409
                    return {"saved": True, "revision": revision}
                if revision != old_revision + 1 or previous[0]["outcome"] != "running":
                    return {"error": "Stale or finished key history"}, 409
                transitions = {
                    "not_configured": set(), "not_attempted": {"attempting"},
                    "attempting": {"succeeded", "rate_limited", "failed"},
                    "succeeded": {"rate_limited", "failed"},
                    "rate_limited": set(), "failed": {"attempting"},
                }
                for row, state in zip(previous, states):
                    if state != row["state"] and state not in transitions[row["state"]]:
                        raise ValueError("Invalid key history transition")
                if any(count < row["job_count"] for count, row in zip(job_counts, previous)):
                    raise ValueError("Groq key job counts cannot decrease")
                increments = [count - row["job_count"] for count, row in zip(job_counts, previous)]
                for row, state, count in zip(previous, states, job_counts):
                    conn.execute("""UPDATE groq_key_history SET state=?,outcome=?,revision=?,job_count=?,
                        updated_at=CASE WHEN state<>? OR job_count<>? THEN ? ELSE updated_at END WHERE id=?""",
                        (state, outcome, revision, count, state, count, now, row["id"]))
            # The revision and daily deltas commit together. Replayed revisions return above.
            for slot, increment in enumerate(increments, 1):
                if increment:
                    conn.execute("""INSERT INTO groq_daily_scores(day_ist,key_slot,job_count)
                        VALUES(?,?,?) ON CONFLICT(day_ist,key_slot)
                        DO UPDATE SET job_count=groq_daily_scores.job_count+excluded.job_count""",
                        (reported_at.astimezone(INDIA).date().isoformat(), slot, increment))
        return {"saved": True, "revision": revision}

    @routes.post("/observations")
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
            if not isinstance(job.get("raw_json"), dict):
                raise ValueError("Invalid job metadata")
            seen.add(job["external_id"])
        conn = connection()
        with run_pipeline.exclusive_cycle(Path(database).parent / "pipeline.lock"), conn:
            conn.execute("BEGIN IMMEDIATE")
            company = conn.execute("SELECT id,name,ats_type,board_slug,career_source FROM companies WHERE id=? AND active=1",
                                   (company_id,)).fetchone()
            if company is None or data.get("fingerprint") != fingerprint(dict(company)):
                return {"error": "Company changed; fetch again"}, 409
            from backend.job_ingestion import deduplicate, mode_for_source
            additive = request.path.endswith('/observations')
            mode = 'observed' if additive else mode_for_source(company['career_source'])
            if data.get('mode', mode) != mode:
                raise ValueError('Ingestion mode does not match company configuration')
            if mode == 'snapshot' and len(seen) != len(jobs):
                raise ValueError('Duplicate snapshot jobs')
            jobs = deduplicate(jobs, mode)
            pipeline.upsert_jobs(conn, company_id, jobs, additive=additive)
        return {"saved": len(jobs)}

    @routes.get("/work")
    def work():
        after = store.integer(request.args.get("after", "0"), 0, 2147483647)
        company_values = request.args.getlist("company_id")
        if len(company_values) > MAX_COMPANIES:
            raise ValueError("Too many candidate companies")
        company_ids = {store.integer(value, 1, 2147483647) for value in company_values}
        conn = connection()
        user = conn.execute("""SELECT u.id,u.resume_text,u.preferences_json FROM users u
            JOIN accounts a ON a.user_id=u.id WHERE u.id>? AND u.active=1
            AND a.email_verified=1 AND trim(u.resume_text)<>'' ORDER BY u.id LIMIT 1""", (after,)).fetchone()
        if user is None:
            return {"items": [], "next": None}
        items = []
        profile = role_titles.current(conn, user)
        candidates = pipeline.filter_candidate_jobs(conn, role_titles.filtering_user(user, profile))
        if company_values:
            # Failed boards must not consume the bounded batch's candidate slots.
            candidates = [job for job in candidates if job["company_id"] in company_ids]
        if not candidates and (profile is not None or conn.execute(
                'SELECT 1 FROM user_settings WHERE user_id=? AND subscriptions_configured=1', (user['id'],)).fetchone()):
            return {"items": [], "next": user["id"]}
        if profile is None:
            return {"items": [], "next": user["id"], "role_profile_required": {
                "user": dict(user), "fingerprint": fingerprint(dict(user)),
                "version": role_titles.VERSION, "model": pipeline.GROQ_MODEL}}
        for job in role_titles.select(candidates, profile)[:MAX_WORK_ITEMS]:
            company = conn.execute("SELECT name FROM companies WHERE id=?", (job["company_id"],)).fetchone()
            items.append(dict(user=dict(user), job=dict(job), company_name=company[0],
                              fingerprint=fingerprint(work_identity(user, job))))
        payload = {"items": items, "next": user["id"]}
        if len(json.dumps(payload).encode()) > MAX_BODY_BYTES:
            raise ValueError("Work batch exceeds transfer limit")
        return payload

    @routes.post("/role-titles")
    def save_role_titles():
        data = body()
        user_id = positive_id(data.get("user_id"))
        if data.get("version") != role_titles.VERSION or data.get("model") != pipeline.GROQ_MODEL:
            return {"error": "Title profile configuration changed"}, 409
        conn = connection()
        with run_pipeline.exclusive_cycle(Path(database).parent / "pipeline.lock"), conn:
            conn.execute("BEGIN IMMEDIATE")
            user = eligible_user(conn, user_id)
            if user is None or data.get("fingerprint") != fingerprint(dict(user)):
                return {"error": "Resume inputs changed; title suggestions discarded"}, 409
            role_titles.save(conn, user, data.get("profile"))
        return {"saved": True}

    @routes.get("/similarity/work")
    def similarity_work():
        after = store.integer(request.args.get("after", "0"), 0, 2147483647)
        job_after = store.integer(request.args.get("job_after", "0"), 0, 2147483647)
        conn = connection()
        if job_after:
            user = eligible_user(conn, after)
        else:
            user = conn.execute("""SELECT u.id,u.resume_text,u.preferences_json FROM users u
                JOIN accounts a ON a.user_id=u.id WHERE u.id>? AND u.active=1
                AND a.email_verified=1 AND trim(u.resume_text)<>'' ORDER BY u.id LIMIT 1""", (after,)).fetchone()
        if user is None:
            if job_after:
                return {"error": "Profile changed; restart similarity scan"}, 409
            return {"items": [], "next": None, "job_after": 0}
        scanned = conn.execute("""SELECT j.* FROM jobs j JOIN companies c ON c.id=j.company_id
            WHERE j.id>? AND j.is_active=1 AND c.active=1
            AND (NOT EXISTS(SELECT 1 FROM user_settings WHERE user_id=? AND subscriptions_configured=1)
                 OR EXISTS(SELECT 1 FROM company_subscriptions WHERE user_id=? AND company_id=c.id))
            ORDER BY j.id LIMIT ?""", (job_after, user["id"], user["id"], MAX_WORK_ITEMS)).fetchall()
        eligible = {job["id"] for job in pipeline.filter_jobs_for_user(scanned, user)}
        items, last = [], job_after
        for job in scanned:
            if job["id"] in eligible:
                saved = conn.execute("""SELECT 1 FROM local_similarity WHERE user_id=? AND job_id=?
                    AND resume_hash=? AND job_hash=? AND preferences_hash=? AND model_revision=? AND scoring_version=?""",
                    (user["id"], job["id"], similarity.text_hash(user["resume_text"]), similarity.job_hash(job),
                     similarity.text_hash(user["preferences_json"]), similarity.MODEL_REVISION, similarity.SCORING_VERSION)).fetchone()
                if not saved:
                    company = conn.execute("SELECT name FROM companies WHERE id=?", (job["company_id"],)).fetchone()
                    item = dict(user=dict(user), job=dict(job), company_name=company[0], fingerprint=fingerprint(work_identity(user, job)))
                    if len(json.dumps(dict(items=[*items, item]), ensure_ascii=False).encode("utf-8")) > MAX_BODY_BYTES - 1024:
                        if not items:
                            raise ValueError("One similarity input exceeds the transfer limit")
                        return {"items": items, "next": user["id"], "job_after": last}
                    items.append(item)
            last = job["id"]
        return {"items": items, "next": user["id"], "job_after": last if len(scanned) == MAX_WORK_ITEMS else 0}

    @routes.post("/similarity")
    def save_similarity():
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
            if not pipeline.filter_jobs_for_user([job], user):
                return {"error": "Job is no longer eligible"}, 409
            value = similarity.validate_result(data.get("result"), user["resume_text"], pipeline.extract_job_description(job))
            conn.execute("""INSERT INTO local_similarity(user_id,job_id,resume_hash,job_hash,preferences_hash,
                model_revision,scoring_version,cosine,skill_overlap,rank_score,matched_skills,jd_skills_not_mentioned,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,job_id) DO UPDATE SET
                resume_hash=excluded.resume_hash,job_hash=excluded.job_hash,preferences_hash=excluded.preferences_hash,
                model_revision=excluded.model_revision,scoring_version=excluded.scoring_version,cosine=excluded.cosine,
                skill_overlap=excluded.skill_overlap,rank_score=excluded.rank_score,matched_skills=excluded.matched_skills,
                jd_skills_not_mentioned=excluded.jd_skills_not_mentioned,updated_at=excluded.updated_at""",
                (user_id, job_id, similarity.text_hash(user["resume_text"]), similarity.job_hash(job),
                 similarity.text_hash(user["preferences_json"]), value["model_revision"], value["scoring_version"],
                 value["cosine"], value["skill_overlap"], value["rank_score"], json.dumps(value["matched_skills"]),
                 json.dumps(value["jd_skills_not_mentioned"]), datetime.now(timezone.utc).isoformat()))
        return {"saved": True}

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
            profile = role_titles.current(conn, user)
            if profile is None or role_titles.match_index(job["title"], profile) is None:
                return {"error": "Job title requires review; result discarded"}, 409
            filtering_user = role_titles.filtering_user(user, profile)
            if not pipeline.filter_jobs_for_user([job], filtering_user):
                return {"error": "Job preferences changed; result discarded"}, 409
            existing = conn.execute("SELECT is_stale FROM scores WHERE user_id=? AND job_id=?", (user_id, job_id)).fetchone()
            if existing and not existing[0]:
                return {"saved": False, "already_scored": True}
            result = data.get("result")
            validate_evaluation(result, user["resume_text"], pipeline.extract_job_description(job))
            task = conn.execute("SELECT revision FROM matching_tasks WHERE user_id=?", (user_id,)).fetchone()
            pipeline.store_score(conn, job_id, user_id, result)
            if task and not pipeline.filter_candidate_jobs(conn, filtering_user):
                conn.execute("""UPDATE matching_tasks SET status='complete',last_error=NULL
                    WHERE user_id=? AND revision=?""", (user_id, task[0]))
        return {"saved": True}

    @routes.post("/job-unavailable")
    def job_unavailable():
        """Retain history while retiring a worker-verified absent PwC posting.

        The worker must first receive a detail 403 and an empty official search
        for that requisition. A generic network failure is never evidence.
        """
        data = body()
        user_id, job_id = positive_id(data.get('user_id')), positive_id(data.get('job_id'))
        if data.get('reason') != 'pwc_detail_403_search_empty':
            raise ValueError('Unsupported unavailability evidence')
        conn = connection()
        with run_pipeline.exclusive_cycle(Path(database).parent / 'pipeline.lock'), conn:
            conn.execute('BEGIN IMMEDIATE')
            user = eligible_user(conn, user_id)
            job = conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if (user is None or job is None or
                    data.get('fingerprint') != fingerprint(work_identity(user, dict(job, is_active=1)))):
                return {'error': 'Inputs changed; unavailability discarded'}, 409
            if job['company_id'] not in matching_tasks.companies_for_user(conn, user_id):
                return {'error': 'Subscription changed; unavailability discarded'}, 409
            company = conn.execute('SELECT career_source,active FROM companies WHERE id=?', (job['company_id'],)).fetchone()
            raw = json.loads(job['raw_json'])
            if (not company['active'] or company['career_source'] != 'pwc' or
                    raw.get('career_source') != 'pwc' or
                    re.fullmatch(r'[0-9]+WD(?:-[0-9]+)?', job['external_id']) is None):
                raise ValueError('Unavailability does not belong to a supported requisition')
            if not job['is_active']:
                return {'saved': True, 'already_inactive': True}
            conn.execute('UPDATE jobs SET is_active=0 WHERE id=?', (job_id,))
            current_app.logger.info('Verified unavailable posting retained as inactive: job_id=%s', job_id)
        return {'saved': True}

    @routes.post("/description")
    def description():
        from backend.career_sources import MAX_DESCRIPTION, SOURCES
        data = body()
        user_id, job_id = positive_id(data.get('user_id')), positive_id(data.get('job_id'))
        text, title, location = data.get('description'), data.get('title'), data.get('location')
        employment = data.get('employment_type')
        title_gate = data.get('title_gate', False)
        if type(title_gate) is not bool:
            raise ValueError('Invalid title gate')
        if (not isinstance(text, str) or not text.strip() or len(text) > MAX_DESCRIPTION or
                not isinstance(title, str) or not title.strip() or len(title) > 1000 or
                location is not None and (not isinstance(location, str) or len(location) > 4000) or
                employment is not None and (not isinstance(employment, str) or len(employment) > 200)):
            raise ValueError('Invalid career description')
        conn = connection()
        with run_pipeline.exclusive_cycle(Path(database).parent / 'pipeline.lock'), conn:
            conn.execute('BEGIN IMMEDIATE')
            user = eligible_user(conn, user_id)
            job = conn.execute('SELECT * FROM jobs WHERE id=? AND is_active=1', (job_id,)).fetchone()
            if user is None or job is None or data.get('fingerprint') != fingerprint(work_identity(user, job)):
                return {'error': 'Inputs changed; description discarded'}, 409
            if job['company_id'] not in matching_tasks.companies_for_user(conn, user_id):
                return {'error': 'Subscription changed; description discarded'}, 409
            company = conn.execute('SELECT career_source,active FROM companies WHERE id=?', (job['company_id'],)).fetchone()
            raw = json.loads(job['raw_json'])
            key = raw.get('career_source')
            if not company['active'] or key not in SOURCES or key != company['career_source'] or SOURCES[key][1] == 'blocked':
                raise ValueError('Description does not belong to a supported career source')
            raw['descriptionHtml'] = text
            raw['resolved_title'], raw['resolved_location'] = title, location
            if employment:
                raw['employmentType'] = employment
            prepared = dict(job, raw_json=json.dumps(raw), title=title, location=location)
            pipeline.extract_job_description(prepared)
            if prepared != dict(job):
                conn.execute('UPDATE jobs SET raw_json=?,title=?,location=? WHERE id=?',
                             (prepared['raw_json'], title, location, job_id))
                conn.execute('UPDATE scores SET is_stale=1 WHERE job_id=?', (job_id,))
                matching_tasks.enqueue_company(conn, job['company_id'], datetime.now(timezone.utc))
            filtering_user = user
            if title_gate:
                profile = role_titles.current(conn, user)
                if profile is None or role_titles.match_index(title, profile) is None:
                    return {'eligible': False, 'job': prepared, 'fingerprint': fingerprint(work_identity(user, prepared))}
                filtering_user = role_titles.filtering_user(user, profile)
            eligible = any(candidate['id'] == job_id for candidate in pipeline.filter_candidate_jobs(conn, filtering_user))
            return {'eligible': eligible, 'job': prepared, 'fingerprint': fingerprint(work_identity(user, prepared))}

    @routes.post("/email")
    @routes.post("/email/immediate")
    @routes.post("/email/matches")
    @routes.post("/email/similarity")
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
            similarity_only = request.path.endswith("/email/similarity")
            if similarity_only:
                from scripts.local_worker import email_text

                similarity.register_sql(conn)
                matches = conn.execute("""SELECT s.*,j.title,j.url,j.location,j.raw_json,c.name company_name """ +
                    similarity.current_where() + """ AND s.rank_score>=?
                    AND NOT EXISTS(SELECT 1 FROM sent_log l WHERE l.user_id=s.user_id
                        AND l.job_id=s.job_id AND l.channel='email')
                    ORDER BY s.rank_score DESC,s.job_id LIMIT 5""",
                    (user["id"], similarity.MODEL_REVISION, similarity.SCORING_VERSION,
                     similarity.EMAIL_THRESHOLD)).fetchall()
                matches = [dict(match) for match in matches]
            else:
                matches = pipeline.get_unsent_top_matches(conn, user["id"], company_ids=refreshed)
            immediate = request.path.endswith(("/email/immediate", "/email/matches", "/email/similarity"))
            matches_only = request.path.endswith(("/email/matches", "/email/similarity"))
            cadence = store.settings(conn, user["id"])["cadence"]
            if cadence not in ("off", "daily", "weekly"):
                raise ValueError("Invalid cadence")
            due = cadence != "off" if immediate else fixed_schedule_due(conn, user["id"], now)
            reminder = immediate and not matches_only and not matches and match_reminders.due(conn, user['id'], now)
            if not due or not (matches or reminder):
                return {"next": user["id"], "sent": False}
            pipeline.validate_email_configuration()
            with pause.open("x", encoding="utf-8") as marker:
                marker.write("Cloud email attempt requires review unless completed.\n")
            try:
                if reminder:
                    match_reminders.deliver(user)
                    match_reminders.mark_sent(conn, user['id'], now)
                    conn.commit()
                elif similarity_only:
                    for match in matches:
                        result = {"model_revision": match["model_revision"],
                            "scoring_version": match["scoring_version"], "cosine": match["cosine"],
                            "skill_overlap": match["skill_overlap"], "rank_score": match["rank_score"],
                            "matched_skills": json.loads(match["matched_skills"]),
                            "jd_skills_not_mentioned": json.loads(match["jd_skills_not_mentioned"])}
                        similarity.validate_result(result, user["resume_text"], pipeline.extract_job_description(match))
                    text = email_text(dict(user), matches, store.settings(conn, user["id"])["include_gaps"])
                    if len(text) > 10000:
                        raise ValueError("Similarity digest exceeds email text limit")
                    pipeline.deliver_account_email(user["email"], "StudentConnect: new local similarity matches", text)
                    pipeline.mark_sent(conn, user["id"], [match["job_id"] for match in matches])
                else:
                    pipeline.deliver_email(user, matches)
                    pipeline.mark_sent(conn, user["id"], [match["job_id"] for match in matches])
                pause.unlink()
            except (smtplib.SMTPException, OSError, ValueError, RuntimeError):
                current_app.logger.error("Cloud digest failed or is uncertain; delivery paused (user_id=%s)", user["id"])
                return {"error": "Delivery uncertain; inspect Gmail and hosted send records"}, 503
            current_app.logger.info("Gmail accepted cloud email (user_id=%s matches=%s reminder=%s)", user["id"], len(matches), reminder)
            return {"next": user["id"], "sent": True}

    @routes.post("/email/onboarding-reminder")
    def onboarding_reminder_email():
        if body():
            raise ValueError("Onboarding reminder request must be empty")
        conn = connection()
        pause = Path(database).parent / "email_schedule.paused"
        now_epoch = int(time.time())
        cutoff = now_epoch - 24 * 60 * 60
        with run_pipeline.exclusive_cycle(Path(database).parent / "pipeline.lock"), conn:
            if pause.exists():
                raise RuntimeError("Email delivery paused; reconcile Gmail and sent_log")
            conn.execute("BEGIN IMMEDIATE")
            candidate = conn.execute("""SELECT email,name,email_verified,resume_missing FROM (
                    SELECT u.email,u.name,a.email_verified,
                        CASE WHEN trim(u.resume_text)='' THEN 1 ELSE 0 END AS resume_missing,
                        CAST(strftime('%s',a.created_at) AS INTEGER) AS created_epoch
                    FROM accounts a JOIN users u ON u.id=a.user_id
                    WHERE u.active=1 AND (a.email_verified=0 OR trim(u.resume_text)='')
                    UNION ALL
                    SELECT t.email,COALESCE(u.name,''),0,
                        CASE WHEN u.id IS NULL OR trim(u.resume_text)='' THEN 1 ELSE 0 END,
                        t.created_at
                    FROM account_tokens t LEFT JOIN users u ON u.id=t.user_id
                    LEFT JOIN accounts a ON a.user_id=u.id
                    WHERE t.purpose='invite' AND t.consumed_at IS NULL AND a.user_id IS NULL
                ) eligible WHERE created_epoch<=?
                    AND NOT EXISTS(SELECT 1 FROM onboarding_reminders r
                        WHERE r.email=eligible.email COLLATE NOCASE)
                ORDER BY created_epoch,email LIMIT 1""", (cutoff,)).fetchone()
            if candidate is None:
                return {"done": True, "sent": False}
            try:
                pipeline.validate_email_configuration()
            except ValueError as error:
                current_app.logger.error("Onboarding reminder email configuration unavailable")
                raise RuntimeError("Onboarding reminder email configuration unavailable") from error
            conn.execute("INSERT INTO onboarding_reminders(email,attempted_at) VALUES(?,?)",
                         (candidate["email"], now_epoch))
            conn.commit()
            try:
                with pause.open("x", encoding="utf-8") as marker:
                    marker.write("Onboarding email attempt requires review unless completed.\n")
                steps = []
                if not candidate["email_verified"]:
                    steps.append("- Verify your email using the sign-up code. If it expired, request a new code on the StudentConnect sign-up page.")
                if candidate["resume_missing"]:
                    steps.append("- Upload your resume from My Resume in StudentConnect.")
                name = candidate["name"].strip() or "there"
                message = (f"Hi {name},\n\n"
                    "You’re almost ready to receive job matches. Please complete the remaining step(s):\n\n"
                    + "\n".join(steps) + "\n\n"
                    "Once your email is verified and your resume is saved, matching can start. "
                    "You’ll be matched against any available company by default; you can choose specific companies later in Settings.\n\n"
                    "If you’ve already completed these steps, you can ignore this message.\n\n"
                    "—StudentConnect")
                pipeline.deliver_account_email(candidate["email"],
                    "Finish setting up your StudentConnect account", message)
                conn.execute("UPDATE onboarding_reminders SET sent_at=? WHERE email=?",
                             (int(time.time()), candidate["email"]))
                conn.commit()
                pause.unlink()
            except (smtplib.SMTPException, OSError, ValueError, RuntimeError):
                current_app.logger.error("Onboarding reminder failed or is uncertain; delivery paused")
                return {"error": "Delivery uncertain; inspect Gmail and hosted send records"}, 503
            current_app.logger.info("Gmail accepted onboarding reminder")
            return {"done": False, "sent": True}

    from backend import web_discovery
    web_discovery.register_routes(routes, connection, database)
    application.register_blueprint(routes)
