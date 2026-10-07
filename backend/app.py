"""Authenticated StudentConnect application; external jobs run separately."""

import hmac
import json
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, flash, g, redirect, render_template, request, session, url_for
from werkzeug.utils import secure_filename

from backend import accounts, hosting, matching_tasks, resumes, store
from backend.pipeline_skeleton import extract_job_description, get_unsent_top_matches

ROOT = Path(__file__).resolve().parents[1]
NAVIGATION = (("dashboard", "Dashboard"), ("jobs", "Job matches"),
              ("resume", "My resume"), ("applications", "Applications"),
              ("analytics", "Analytics"), ("settings_page", "Settings"))


def utc_now():
    return datetime.now(timezone.utc)


def create_app(database_path=None, upload_dir=None):
    origin, hosted, signing_key = hosting.application_settings()
    application = Flask(__name__, template_folder=str(ROOT / "frontend/templates"),
                        static_folder=str(ROOT / "frontend/static"))
    application.config.update(SECRET_KEY=signing_key, SESSION_COOKIE_SECURE=hosted,
                              MAX_CONTENT_LENGTH=resumes.MAX_UPLOAD_BYTES + 65536,
                              SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict")
    database = Path(database_path) if database_path is not None else ROOT / "data/jobs.db"
    uploads = Path(upload_dir) if upload_dir is not None else ROOT / "data/uploads"

    def connection():
        if "database" not in g:
            g.database = sqlite3.connect(database.resolve().as_uri() + "?mode=rw", uri=True, timeout=5)
            g.database.row_factory = sqlite3.Row
            g.database.execute("PRAGMA foreign_keys=ON")
        return g.database

    @application.teardown_appcontext
    def close_database(error):
        conn = g.pop("database", None)
        if conn is not None:
            conn.close()

    def context():
        user = getattr(g, "user", None)
        return dict(user=user, settings=store.settings(connection(), user["id"]) if user else store.DEFAULT_SETTINGS,
                    navigation=NAVIGATION, active=request.endpoint, message=None, matches=[], companies=[],
                    digest=[], trend=[], metrics=None, page=1, pages=1, search="", company=0, min_score=0,
                    location="", job_type="", sort="best", days=0, application_counts={}, stale_count=0,
                    by_company=[], skills=[], saved_searches=[], selected=None)

    def render(page, data, status=200):
        return render_template(page + ".html", script_nonce=getattr(g, "script_nonce", None), **data), status

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    application.jinja_env.globals["csrf_token"] = csrf_token

    @application.before_request
    def protect_local_actions():
        allowed = (origin.removeprefix("https://"),) if hosted else ("localhost", "127.0.0.1")
        if request.host.split(":")[0].lower() not in allowed:
            return "This hostname is not configured for StudentConnect.", 403
        g.user, g.account = None, None
        user_id, version = session.get("user_id"), session.get("session_version")
        if type(user_id) is int and type(version) is int and database.is_file():
            account = connection().execute("SELECT a.* FROM accounts a JOIN users u ON u.id=a.user_id WHERE a.user_id=? AND a.session_version=? AND a.email_verified=1 AND u.active=1",(user_id,version)).fetchone()
            if account:
                g.account = dict(account)
                g.user = dict(connection().execute("SELECT * FROM users WHERE id=?",(user_id,)).fetchone())
        public = {"accounts.login", "accounts.register", "accounts.forgot", "accounts.reset", "static"}
        if request.url_rule is not None and request.endpoint not in public and g.user is None:
            if request.method == "GET":
                return redirect(url_for("accounts.login"))
            return "Sign in before making changes.", 401
        if request.endpoint in {"settings_page", "accounts.login", "accounts.register", "accounts.forgot", "accounts.reset"}:
            g.script_nonce = secrets.token_urlsafe(32)
        if request.method == "POST" and request.url_rule is not None:
            token = request.form.get("csrf_token", "")
            expected = session.get("csrf", "")
            if not token or not expected or not hmac.compare_digest(token, expected):
                return "The form expired. Reload the page and try again.", 403

    @application.after_request
    def protect_response(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'self' 'unsafe-inline'; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        )
        if getattr(g, "script_nonce", None):
            response.headers["Content-Security-Policy"] += (
                f"; script-src 'nonce-{g.script_nonce}'; script-src-attr 'none'"
            )
        return response

    @application.errorhandler(413)
    def too_large(error):
        return "The upload is too large. Resume files must be at most 2 MB.", 413

    @application.errorhandler(store.MultipleProfiles)
    def multiple_profiles(error):
        data = dict(user=None, navigation=NAVIGATION, active=request.endpoint,
                    message="This dashboard requires exactly one active profile. Multiple profiles are active.")
        return render("empty", data, 409)

    def database_error(error):
        application.logger.error("Web data operation failed (%s)", type(error).__name__)
        return render("empty", dict(user=None, navigation=NAVIGATION, active=request.endpoint,
            message="Saved dashboard data could not be loaded. Check the local database and application logs."), 503)

    application.register_error_handler(sqlite3.Error, database_error)
    application.register_error_handler(ValueError, database_error)
    application.register_error_handler(TypeError, database_error)
    application.register_error_handler(OSError, database_error)

    def populate(data):
        if not data["user"]:
            data["message"] = ("No active profile yet. Create your profile in Settings." if database.is_file()
                               else "No job database yet. Run the documented local migration command first.")
            return
        conn = connection()
        user_id = data["user"]["id"]
        data.update(store.summary(conn, user_id, utc_now()))
        data["companies"] = conn.execute("""SELECT DISTINCT c.id,c.name FROM scores s
            JOIN jobs j ON j.id=s.job_id JOIN companies c ON c.id=j.company_id
            WHERE s.user_id=? AND s.is_stale=0 AND j.is_active=1 AND c.active=1 ORDER BY c.name,c.id LIMIT 1000""", (user_id,)).fetchall()
        data["digest"] = [store.match(row) for row in get_unsent_top_matches(conn, user_id)]

    @application.get("/")
    def dashboard():
        data = context()
        try:
            data.update(store.filters(request.args))
        except ValueError as error:
            data["message"] = str(error)
            return render("dashboard", data, 400)
        populate(data)
        if data["user"]:
            data["matches"], data["pages"] = store.matches(connection(), data["user"]["id"], data, utc_now())
            data["target_companies"] = connection().execute("""SELECT c.id,c.name,c.ats_type FROM company_subscriptions sub
                JOIN companies c ON c.id=sub.company_id WHERE sub.user_id=? ORDER BY c.name,c.id LIMIT 1000""", (data["user"]["id"],)).fetchall()
        return render("dashboard", data)

    @application.get("/jobs")
    def jobs():
        data = context()
        try:
            data.update(store.filters(request.args))
            selected_id = store.integer(request.args.get("job", "0"), 0, 2147483647)
        except ValueError as error:
            data["message"] = str(error)
            return render("jobs", data, 400)
        populate(data)
        if data["user"]:
            conn = connection()
            user_id = data["user"]["id"]
            data["matches"], data["pages"] = store.matches(conn, user_id, data, utc_now())
            data["saved_searches"] = conn.execute("SELECT id,name,filters_json FROM saved_searches WHERE user_id=? ORDER BY name LIMIT 50", (user_id,)).fetchall()
            data["saved_searches"] = [dict(row, link=url_for("jobs", **json.loads(row["filters_json"]))) for row in data["saved_searches"]]
            if selected_id:
                row = conn.execute("SELECT " + store.MATCH_COLUMNS + " " + store.MATCH_FROM + " AND j.id=?", (user_id, user_id, user_id, selected_id)).fetchone()
                if not row:
                    data["message"] = "This match is unavailable or needs rescoring."
                    return render("jobs", data, 404)
                data["selected"] = store.match(row)
            elif data["matches"]:
                data["selected"] = data["matches"][0]
            if data["selected"]:
                try:
                    data["selected"]["description"] = extract_job_description(data["selected"])
                except ValueError:
                    application.logger.warning("Job description unavailable (job_id=%s)", data["selected"]["job_id"])
                    data["selected"]["description"] = "The saved posting has no usable description. Read the source posting before applying."
        return render("jobs", data)

    @application.post("/jobs/searches")
    def save_search():
        data = context()
        if not data["user"]:
            return redirect(url_for("settings_page"))
        try:
            selected = store.filters(request.form)
            name = request.form.get("name", "").strip()
            if not name or len(name) > 80:
                raise ValueError("Give the search a name of at most 80 characters")
            query = dict(q=selected["search"], company=selected["company"], min_score=selected["min_score"],
                         location=selected["location"], job_type=selected["job_type"], sort=selected["sort"], days=selected["days"])
            conn = connection()
            with conn:
                conn.execute("""INSERT INTO saved_searches(user_id,name,filters_json,updated_at) VALUES(?,?,?,?)
                    ON CONFLICT(user_id,name) DO UPDATE SET filters_json=excluded.filters_json,updated_at=excluded.updated_at""",
                    (data["user"]["id"], name, json.dumps(query), utc_now().isoformat()))
        except ValueError as error:
            data["message"] = str(error)
            return render("empty", data, 400)
        flash("Search saved.")
        return redirect(url_for("jobs", **query))

    @application.route("/settings", methods=["GET", "POST"])
    def settings_page():
        data = context()
        conn = connection() if database.is_file() else None
        data["available_companies"] = conn.execute("SELECT id,name,ats_type,board_slug FROM companies WHERE active=1 ORDER BY name,id LIMIT 1000").fetchall() if conn else []
        data["subscriptions"] = {row[0] for row in conn.execute("SELECT company_id FROM company_subscriptions WHERE user_id=?", (data["user"]["id"],))} if data["user"] else set()
        data["preferences"] = json.loads(data["user"]["preferences_json"] or "{}") if data["user"] else {}
        data["location_suggestions"] = store.location_suggestions(conn) if conn else []
        if request.method == "GET":
            if not conn:
                data["message"] = "Run the documented migration command to initialize the database first."
            return render("settings", data)
        if not conn:
            return render("settings", dict(data, message="Initialize the local database first."), 409)
        try:
            name, email = request.form.get("name", "").strip(), request.form.get("email", "").strip()
            university = request.form.get("university", "").strip()
            if not name or len(name) > 100 or len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(university) > 200:
                raise ValueError("Provide a name and valid email, within the field length limits")
            if email.lower() != data["user"]["email"].lower():
                raise ValueError("Your verified account email cannot be changed in Settings")
            year = request.form.get("graduation_year", "").strip()
            year = store.integer(year, 1950, 2100) if year else None
            cadence = request.form.get("cadence", "daily")
            send_time = request.form.get("send_time", "08:00")
            zone = request.form.get("timezone", "Asia/Kolkata")
            if cadence not in ("daily", "weekly", "off") or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", send_time) or zone not in ("Asia/Kolkata", "UTC"):
                raise ValueError("Invalid email schedule preference")
            minimum = store.integer(request.form.get("min_score", "60"), 0, 100)
            company_ids = {store.integer(value, 1, 2147483647) for value in request.form.getlist("companies")}
            if not company_ids.issubset({row["id"] for row in data["available_companies"]}):
                raise ValueError("Select companies from the available list")
            preferences = {}
            for key in ("roles", "locations"):
                raw = ",".join(request.form.getlist(key)) if key == "locations" else request.form.get(key, "")
                if len(raw) > 1000:
                    raise ValueError("Job preferences are too long")
                values = [value.strip() for value in raw.split(",") if value.strip()]
                if len(values) > 20 or any(len(value) > 100 for value in values):
                    raise ValueError("Use at most 20 short role/location preferences")
                preferences[key] = values
            stipend = request.form.get("min_stipend", "").strip()
            preferences["min_stipend"] = store.integer(stipend, 0, 1000000) if stipend else None
            types = request.form.getlist("employment_types")
            if any(value not in ("internship", "full-time", "part-time") for value in types):
                raise ValueError("Invalid employment preference")
            preferences["employment_types"] = types
            matching_changed = preferences != data["preferences"] or company_ids != data["subscriptions"]
            with conn:
                user_id = data["user"]["id"]
                conn.execute("UPDATE users SET name=?,preferences_json=? WHERE id=?", (name, json.dumps(preferences), user_id))
                if preferences != data["preferences"]:
                    conn.execute("UPDATE scores SET is_stale=1 WHERE user_id=?", (user_id,))
                conn.execute("""INSERT INTO user_settings(user_id,university,graduation_year,cadence,send_time,timezone,min_score,include_gaps,subscriptions_configured)
                    VALUES(?,?,?,?,?,?,?,?,1) ON CONFLICT(user_id) DO UPDATE SET
                    university=excluded.university,graduation_year=excluded.graduation_year,cadence=excluded.cadence,
                    send_time=excluded.send_time,timezone=excluded.timezone,min_score=excluded.min_score,
                    include_gaps=excluded.include_gaps,subscriptions_configured=1""",
                    (user_id, university, year, cadence, send_time, zone, minimum, int(request.form.get("include_gaps") == "on")))
                conn.execute("DELETE FROM company_subscriptions WHERE user_id=?", (user_id,))
                conn.executemany("INSERT INTO company_subscriptions(user_id,company_id,subscribed_at) VALUES(?,?,?)",
                                 [(user_id, company_id, utc_now().isoformat()) for company_id in company_ids])
                if matching_changed:
                    matching_tasks.enqueue(conn, user_id, 'preferences' if preferences != data['preferences'] else 'companies', utc_now())
        except ValueError as error:
            data["message"] = str(error)
            return render("settings", data, 400)
        except sqlite3.IntegrityError:
            data["message"] = "The profile could not be saved. Check whether that email is already used."
            return render("settings", data, 409)
        flash("Profile, company choices, and preferences saved.")
        return redirect(url_for("settings_page"))

    @application.route("/resume", methods=["GET", "POST"])
    def resume():
        data = context()
        populate(data)
        if data["user"]:
            conn = connection()
            data["versions"] = conn.execute("SELECT id,source_name,created_at FROM resume_versions WHERE user_id=? ORDER BY id DESC LIMIT 10", (data["user"]["id"],)).fetchall()
        else:
            data["versions"] = []
        if request.method == "GET":
            return render("resume", data)
        if not data["user"]:
            return redirect(url_for("settings_page"))
        try:
            if request.form.get("consent") != "on":
                raise ValueError("Confirm local storage and use of your resume for matching")
            upload = request.files.get("resume_file")
            content, suffix = None, None
            if upload and upload.filename:
                text, content, suffix = resumes.read_upload(upload)
                source_name = secure_filename(upload.filename)[:200] or "Uploaded resume"
            else:
                text = resumes.validate_text(request.form.get("resume_text", ""))
                source_name = "Text editor"
            changed = resumes.save_version(conn, data["user"], text, source_name, utc_now(), uploads, content, suffix)
        except ValueError as error:
            data["message"] = str(error)
            return render("resume", data, 400)
        flash("Resume saved. Previous scores are retained but need rescoring." if changed else "Resume is unchanged.")
        return redirect(url_for("resume"))

    @application.route("/applications", methods=["GET", "POST"])
    def applications():
        data = context()
        populate(data)
        data["statuses"] = store.STATUSES
        data["application_rows"] = []
        if not data["user"]:
            return render("applications", data)
        conn, user_id = connection(), data["user"]["id"]
        if request.method == "POST":
            try:
                with conn:
                    store.save_application(conn, user_id, request.form, utc_now())
            except ValueError as error:
                data["message"] = str(error)
                return render("applications", data, 400)
            flash("Application saved. Applying on the company's website is a separate action.")
            return redirect(url_for("applications"))
        rows = conn.execute("""SELECT a.*,s.fit_score FROM applications a
            LEFT JOIN scores s ON s.job_id=a.job_id AND s.user_id=a.user_id AND s.is_stale=0
            WHERE a.user_id=? ORDER BY a.updated_at DESC,a.id DESC LIMIT 200""", (user_id,)).fetchall()
        data["application_rows"] = [dict(row, url=store.safe_url(row["url"] or "")) for row in rows]
        return render("applications", data)

    @application.get("/analytics")
    def analytics():
        data = context()
        try:
            weeks = store.integer(request.args.get("weeks", "8"), 1, 26)
        except ValueError as error:
            return render("analytics", dict(data, message=str(error)), 400)
        populate(data)
        data["weeks"] = weeks
        if data["user"]:
            data.update(store.summary(connection(), data["user"]["id"], utc_now(), weeks))
            applied = sum(data["application_counts"][status] for status in store.STATUSES if status != "saved")
            replies = sum(data["application_counts"][status] for status in ("interview", "offer", "rejected"))
            data["response_rate"] = round(100 * replies / applied, 1) if applied else None
        return render("analytics", data)

    @application.context_processor
    def account_navigation():
        user = getattr(g, 'user', None)
        return dict(is_account_admin=bool(getattr(g,"account",None) and g.account["is_admin"]),
                    matching_status=matching_tasks.status(connection(), user['id']) if user else None)

    accounts.register_routes(application, connection, context, lambda: utc_now())
    return application


app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
