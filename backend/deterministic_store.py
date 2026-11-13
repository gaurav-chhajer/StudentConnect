"""Private SQLite cache and audit records for deterministic matching."""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone


def content_hash(text):
    if not isinstance(text, str) or not text:
        raise ValueError("Cached text must be nonempty")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def initialize(conn):
    with conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS deterministic_scores (
            resume_hash TEXT NOT NULL, jd_hash TEXT NOT NULL, version TEXT NOT NULL,
            result_json TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(resume_hash,jd_hash,version))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS parsed_jds (
            jd_hash TEXT PRIMARY KEY, requirements_json TEXT NOT NULL, created_at TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS deterministic_explanations (
            resume_hash TEXT NOT NULL, jd_hash TEXT NOT NULL, version TEXT NOT NULL,
            explanation_json TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(resume_hash,jd_hash,version))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS deterministic_llm_calls (
            id INTEGER PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL,
            latency_ms INTEGER NOT NULL CHECK(latency_ms >= 0),
            input_tokens INTEGER, output_tokens INTEGER, status TEXT NOT NULL,
            created_at TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS deterministic_user_llm_requests (
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL CHECK(user_id > 0),
            created_at TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS unmatched_skills (
            skill TEXT PRIMARY KEY, count INTEGER NOT NULL CHECK(count > 0), first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL)""")


def get_score(conn, resume_hash, jd_hash, version):
    row = conn.execute("SELECT result_json FROM deterministic_scores WHERE resume_hash=? AND jd_hash=? AND version=?",
                       (resume_hash, jd_hash, version)).fetchone()
    if row is None:
        return None
    result = json.loads(row[0])
    if not isinstance(result, dict) or type(result.get("score")) is not int:
        raise ValueError("Invalid cached deterministic score")
    return result


def put_score(conn, resume_hash, jd_hash, version, result):
    if not isinstance(result, dict) or type(result.get("score")) is not int or not 0 <= result["score"] <= 100:
        raise ValueError("Invalid deterministic score result")
    now = datetime.now(timezone.utc).isoformat()
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with conn:
        conn.execute("""INSERT INTO deterministic_scores VALUES(?,?,?,?,?)
            ON CONFLICT(resume_hash,jd_hash,version) DO UPDATE SET result_json=excluded.result_json,
            created_at=excluded.created_at""", (resume_hash, jd_hash, version, encoded, now))


def get_explanation(conn, resume_hash, jd_hash, version):
    row = conn.execute("""SELECT explanation_json FROM deterministic_explanations
        WHERE resume_hash=? AND jd_hash=? AND version=?""", (resume_hash, jd_hash, version)).fetchone()
    if row is None:
        return None
    result = json.loads(row[0])
    if (not isinstance(result, dict) or set(result) != {"explanation", "gap_summary", "explanation_source"} or
            result["explanation_source"] != "llm" or
            any(not isinstance(result[field], str) or not result[field].strip() or len(result[field]) > 800
                for field in ("explanation", "gap_summary"))):
        raise ValueError("Invalid cached LLM explanation")
    return result


def put_explanation(conn, resume_hash, jd_hash, version, explanation):
    if (not isinstance(explanation, dict) or set(explanation) !=
            {"explanation", "gap_summary", "explanation_source"} or explanation["explanation_source"] != "llm" or
            any(not isinstance(explanation[field], str) or not explanation[field].strip() or len(explanation[field]) > 800
                for field in ("explanation", "gap_summary"))):
        raise ValueError("Invalid LLM explanation result")
    now = datetime.now(timezone.utc).isoformat()
    encoded = json.dumps(explanation, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with conn:
        conn.execute("""INSERT INTO deterministic_explanations VALUES(?,?,?,?,?)
            ON CONFLICT(resume_hash,jd_hash,version) DO NOTHING""", (resume_hash, jd_hash, version, encoded, now))


def get_jd_requirements(conn, jd_hash):
    row = conn.execute("SELECT requirements_json FROM parsed_jds WHERE jd_hash=?", (jd_hash,)).fetchone()
    return json.loads(row[0]) if row else None


def put_jd_requirements(conn, jd_hash, requirements):
    now = datetime.now(timezone.utc).isoformat()
    encoded = json.dumps(requirements, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with conn:
        conn.execute("INSERT INTO parsed_jds VALUES(?,?,?) ON CONFLICT(jd_hash) DO NOTHING",
                     (jd_hash, encoded, now))


def note_unmatched(conn, skills):
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        for skill in sorted(set(skills)):
            if not isinstance(skill, str) or not skill.strip() or len(skill) > 100:
                raise ValueError("Unmatched skill must be bounded text")
            conn.execute("""INSERT INTO unmatched_skills VALUES(?,1,?,?)
                ON CONFLICT(skill) DO UPDATE SET count=count+1,last_seen=excluded.last_seen""",
                (skill.strip(), now, now))


def pending_alias_candidates(conn, *, minimum_count=2, limit=100):
    if type(minimum_count) is not int or not 1 <= minimum_count <= 1000000:
        raise ValueError("Minimum review count is out of range")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Review limit is out of range")
    rows = conn.execute("""SELECT skill,count,first_seen,last_seen FROM unmatched_skills
        WHERE count>=? ORDER BY count DESC,skill COLLATE NOCASE LIMIT ?""", (minimum_count, limit)).fetchall()
    return [dict(skill=row[0], count=row[1], first_seen=row[2], last_seen=row[3]) for row in rows]
