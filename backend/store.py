"""User-scoped SQLite queries for the local web application."""

import json
import re
from datetime import timedelta
from backend.evaluation import validate_evaluation
from backend.locations import matches as location_matches
from urllib.parse import urlsplit

PAGE_SIZE = 20
STATUSES = ("saved", "applied", "screen", "assessment", "interview", "offer", "rejected", "no_reply")
DEFAULT_SETTINGS = dict(university="", graduation_year=None, cadence="daily", send_time="08:00",
                        timezone="Asia/Kolkata", min_score=60, include_gaps=1, subscriptions_configured=0)
MATCH_FROM = """FROM scores s JOIN jobs j ON j.id=s.job_id JOIN companies c ON c.id=j.company_id
    WHERE s.user_id=? AND s.is_stale=0 AND j.is_active=1 AND c.active=1
    AND (NOT EXISTS (SELECT 1 FROM user_settings WHERE user_id=? AND subscriptions_configured=1)
         OR EXISTS (SELECT 1 FROM company_subscriptions WHERE user_id=? AND company_id=c.id))"""
MATCH_COLUMNS = """j.id job_id,j.title,j.location,j.url,j.first_seen_at,j.raw_json,
    c.id company_id,c.name company_name,s.fit_score,s.reasoning,s.matched_skills,s.missing_requirements,
    s.evaluation_json,s.deterministic_json"""


class MultipleProfiles(ValueError):
    """A local single-user app cannot choose between active profiles."""


def profile(conn):
    rows = conn.execute("SELECT * FROM users WHERE active=1 LIMIT 2").fetchall()
    if len(rows) > 1:
        raise MultipleProfiles("Multiple profiles are active")
    return dict(rows[0]) if rows else None


def settings(conn, user_id):
    row = conn.execute("SELECT * FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
    return dict(row) if row else dict(DEFAULT_SETTINGS)


def location_suggestions(conn):
    """Bounded location terms and labels drawn from active company postings."""
    rows = conn.execute("""SELECT j.location,COUNT(*) count FROM jobs j
        JOIN companies c ON c.id=j.company_id WHERE j.is_active=1 AND c.active=1
        AND length(j.location) BETWEEN 1 AND 1000 GROUP BY j.location
        ORDER BY count DESC,j.location LIMIT 500""").fetchall()
    terms = {}
    for row in rows:
        # ATS locations commonly separate cities/states/countries by commas;
        # semicolons, slashes and pipes also separate multiple office locations.
        for term in dict.fromkeys(part.strip() for part in re.split(r"[,;/|]", row["location"]) if part.strip()):
            if len(term) > 100:
                continue
            key = term.casefold()
            example = row["location"][:200]
            if key not in terms:
                terms[key] = dict(value=term, example=example, count=0)
            terms[key]["count"] += row["count"]
            if "," not in terms[key]["example"] and "," in example:
                terms[key]["example"] = example
    return sorted(terms.values(), key=lambda item: (-item["count"], item["value"].casefold()))[:500]


def integer(value, minimum, maximum):
    if not isinstance(value, str) or len(value) > 10 or not value.isascii() or not value.isdecimal():
        raise ValueError("Use a valid whole number")
    number = int(value)
    if not minimum <= number <= maximum:
        raise ValueError(f"Number must be between {minimum} and {maximum}")
    return number


def skill_list(value):
    items = json.loads(value or "[]")
    if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
        raise ValueError("Invalid saved skill list")
    return items


def safe_url(value):
    if not isinstance(value, str) or any(ord(char) < 32 for char in value) or len(value) > 2000:
        return None
    parsed = urlsplit(value)
    return value if parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username else None


def match(row):
    item = dict(row)
    if type(item["fit_score"]) is not int or not 0 <= item["fit_score"] <= 100:
        raise ValueError("Invalid saved score")
    item["matched_skills"] = skill_list(item["matched_skills"])
    item["missing_requirements"] = skill_list(item["missing_requirements"])
    item["evaluation"] = validate_evaluation(json.loads(item["evaluation_json"])) if item.get("evaluation_json") else None
    item["deterministic"] = _deterministic_breakdown(item.get("deterministic_json"))
    item["url"] = safe_url(item["url"] or "")
    return item


def _deterministic_breakdown(value):
    if not value:
        return None
    result = json.loads(value)
    if (isinstance(result, dict) and "weights" not in result and
            result.get("scorer_version") == "deterministic-v2" and
            result.get("weights_version") == "weights-v1"):
        from backend.deterministic_scoring import WEIGHTS
        result["weights"] = dict(WEIGHTS)
    component_names = {"must_have", "nice_to_have", "semantic", "experience", "education_title"}
    if (not isinstance(result, dict) or type(result.get("score")) is not int or
            not 0 <= result["score"] <= 100 or not isinstance(result.get("components"), dict) or
            set(result["components"]) != component_names or not isinstance(result.get("component_points"), dict) or
            set(result["component_points"]) != component_names or not isinstance(result.get("weights"), dict) or
            set(result["weights"]) != component_names or not isinstance(result.get("matched"), list) or
            not isinstance(result.get("missing_must_have_requirements"), list)):
        raise ValueError("Invalid saved deterministic score breakdown")
    for name in component_names:
        if (type(result["components"][name]) not in (int, float) or not 0 <= result["components"][name] <= 1 or
                type(result["component_points"][name]) not in (int, float) or
                type(result["weights"][name]) is not int or result["weights"][name] < 0):
            raise ValueError("Invalid saved deterministic score component")
    for item in result["matched"]:
        if not isinstance(item, dict) or any(not isinstance(item.get(key), str) for key in ("jd", "resume", "type")):
            raise ValueError("Invalid saved deterministic match evidence")
    if not all(isinstance(item, dict) and isinstance(item.get("jd"), str)
               for item in result["missing_must_have_requirements"]):
        raise ValueError("Invalid saved deterministic requirement evidence")
    return result


def like_pattern(value):
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def filters(args):
    result = dict(page=integer(args.get("page", "1"), 1, 1000000),
                  company=integer(args.get("company", "0"), 0, 2147483647),
                  min_score=integer(args.get("min_score", "0"), 0, 100),
                  search=args.get("q", "").strip(), location=args.get("location", "").strip(),
                  job_type=args.get("job_type", ""), sort=args.get("sort", "best"),
                  days=integer(args.get("days", "0"), 0, 365),
                  only_mine=integer(args.get("only_mine", "0"), 0, 1))
    if len(result["search"]) > 200 or len(result["location"]) > 100:
        raise ValueError("Search or location is too long")
    if result["job_type"] not in ("", "internship", "full-time", "part-time") or result["sort"] not in ("best", "recent"):
        raise ValueError("Unsupported job filter")
    return result


def matches(conn, user_id, selected_filters, now):
    conn.create_function("location_matches",2,location_matches,deterministic=True)
    params = [user_id] * 3
    where = MATCH_FROM + " AND s.fit_score>=? AND (?=0 OR c.id=?)"
    if selected_filters.get('only_mine'):
        where += " AND EXISTS(SELECT 1 FROM company_subscriptions mine WHERE mine.user_id=s.user_id AND mine.company_id=c.id)"
    params.extend((selected_filters["min_score"], selected_filters["company"], selected_filters["company"]))
    pattern = like_pattern(selected_filters["search"])
    where += " AND (j.title LIKE ? ESCAPE '\\' OR c.name LIKE ? ESCAPE '\\' OR s.matched_skills LIKE ? ESCAPE '\\')"
    params.extend((pattern,) * 3)
    if selected_filters["location"]:
        where += " AND location_matches(j.location,?)=1"
        params.append(selected_filters["location"])
    if selected_filters["days"]:
        where += " AND julianday(j.first_seen_at)>=julianday(?)"
        params.append((now - timedelta(days=selected_filters["days"])).isoformat())
    if selected_filters["job_type"]:
        where += " AND lower(COALESCE(json_extract(j.raw_json,'$.employmentType'),json_extract(j.raw_json,'$.categories.commitment'),'')) LIKE ?"
        params.append({"internship": "%intern%", "full-time": "%full%", "part-time": "%part%"}[selected_filters["job_type"]])
    count = conn.execute("SELECT COUNT(*) " + where, params).fetchone()[0]
    order = "s.fit_score DESC,j.id ASC" if selected_filters["sort"] == "best" else "j.first_seen_at DESC,j.id DESC"
    if selected_filters.get("company_cards") and not selected_filters["company"]:
        company_count = conn.execute("SELECT COUNT(DISTINCT c.id) " + where, params).fetchone()[0]
        company_order = "MAX(s.fit_score) DESC,c.id ASC" if selected_filters["sort"] == "best" else "MAX(j.first_seen_at) DESC,c.id ASC"
        company_ids = [row[0] for row in conn.execute(
            "SELECT c.id " + where + " GROUP BY c.id ORDER BY " + company_order + " LIMIT 6 OFFSET ?",
            (*params, (selected_filters["page"] - 1) * 6))]
        selected_filters["company_count"] = company_count
        selected_filters["opening_count"] = count
        if not company_ids:
            return [], max(1, (company_count + 5) // 6)
        placeholders = ",".join("?" for _ in company_ids)
        rows = conn.execute("SELECT * FROM (SELECT " + MATCH_COLUMNS +
            ",ROW_NUMBER() OVER(PARTITION BY c.id ORDER BY " + order + ") card_rank," +
            "COUNT(*) OVER(PARTITION BY c.id) company_openings " + where +
            " AND c.id IN (" + placeholders + ")) WHERE card_rank<=5", (*params, *company_ids)).fetchall()
        rank = {company_id: index for index, company_id in enumerate(company_ids)}
        rows = sorted(rows, key=lambda row: (rank[row["company_id"]], row["card_rank"]))
        return [match(row) for row in rows], max(1, (company_count + 5) // 6)
    rows = conn.execute("SELECT " + MATCH_COLUMNS + " " + where + " ORDER BY " + order + " LIMIT ? OFFSET ?",
                        (*params, PAGE_SIZE, (selected_filters["page"] - 1) * PAGE_SIZE)).fetchall()
    return [match(row) for row in rows], max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE)


def summary(conn, user_id, now, weeks=8):
    params = (user_id,) * 3
    metrics = dict(conn.execute("SELECT COUNT(*) total,ROUND(AVG(s.fit_score),1) average,"
        "COALESCE(MAX(s.fit_score),0) best_score,COALESCE(SUM(s.fit_score>=80),0) strong_matches,"
        "COALESCE(SUM(julianday(s.scored_at)>=julianday(?)),0) recent " + MATCH_FROM,
        ((now - timedelta(days=7)).isoformat(), *params)).fetchone())
    trend = conn.execute("SELECT strftime('%Y-W%W',s.scored_at) week,ROUND(AVG(s.fit_score),1) average,COUNT(*) count " + MATCH_FROM +
                         " AND julianday(s.scored_at)>=julianday(?) GROUP BY week ORDER BY week",
                         (*params, (now - timedelta(weeks=weeks)).isoformat())).fetchall()
    by_company = conn.execute("SELECT c.name,COUNT(*) count,ROUND(AVG(s.fit_score),1) average " + MATCH_FROM +
                              " GROUP BY c.id ORDER BY count DESC,c.name LIMIT 20", params).fetchall()
    application_counts = {status: 0 for status in STATUSES}
    for row in conn.execute("SELECT status,COUNT(*) count FROM applications WHERE user_id=? GROUP BY status", (user_id,)):
        application_counts[row["status"]] = row["count"]
    # JSON expansion is bounded by saved matching data and returns only the top skills.
    skill_counts = conn.execute("""SELECT skill,COUNT(*) count FROM (
        SELECT DISTINCT s.id,value skill FROM scores s JOIN jobs j ON j.id=s.job_id
        JOIN companies c ON c.id=j.company_id JOIN json_each(s.matched_skills)
        WHERE s.user_id=? AND s.is_stale=0 AND j.is_active=1 AND c.active=1
        AND (NOT EXISTS(SELECT 1 FROM user_settings WHERE user_id=? AND subscriptions_configured=1)
          OR EXISTS(SELECT 1 FROM company_subscriptions WHERE user_id=? AND company_id=c.id))
        ) GROUP BY skill ORDER BY count DESC,skill LIMIT 15""", params).fetchall()
    return dict(metrics=metrics, trend=trend, by_company=by_company, application_counts=application_counts,
                skills=skill_counts,
                stale_count=conn.execute("SELECT COUNT(*) FROM scores WHERE user_id=? AND is_stale=1", (user_id,)).fetchone()[0])


def save_application(conn, user_id, form, now):
    status = form.get("status", "saved")
    notes = form.get("notes", "").strip()
    if status not in STATUSES or len(notes) > 2000:
        raise ValueError("Invalid application status or notes")
    application_id = integer(form.get("application_id", "0"), 0, 2147483647)
    timestamp = now.isoformat()
    if application_id:
        current = conn.execute("SELECT * FROM applications WHERE id=? AND user_id=?", (application_id, user_id)).fetchone()
        if not current:
            raise ValueError("Application not found")
        applied_at = current["applied_at"] or (timestamp if status != "saved" else None)
        conn.execute("UPDATE applications SET status=?,notes=?,applied_at=?,updated_at=? WHERE id=? AND user_id=?",
                     (status, notes, applied_at, timestamp, application_id, user_id))
        return
    job_id = integer(form.get("job_id", "0"), 0, 2147483647)
    if job_id:
        job = conn.execute("SELECT j.title,j.url,c.name company_name FROM jobs j JOIN companies c ON c.id=j.company_id WHERE j.id=?", (job_id,)).fetchone()
        if not job:
            raise ValueError("Job not found")
        title, company_name, url = job["title"], job["company_name"], safe_url(job["url"] or "")
        existing = conn.execute("SELECT id FROM applications WHERE user_id=? AND job_id=?", (user_id, job_id)).fetchone()
        if existing:
            # Repeated Save clicks must not reset an existing application's stage or notes.
            return
    else:
        title = form.get("title", "").strip()
        company_name = form.get("company_name", "").strip()
        supplied_url = form.get("url", "").strip()
        url = safe_url(supplied_url)
        if not title or len(title) > 200 or not company_name or len(company_name) > 200 or (supplied_url and not url):
            raise ValueError("Provide a role, company, and valid optional HTTP/HTTPS URL")
    conn.execute("""INSERT INTO applications(user_id,job_id,title,company_name,url,status,notes,applied_at,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)""", (user_id, job_id or None, title, company_name, url, status, notes,
                                          timestamp if status != "saved" else None, timestamp, timestamp))
