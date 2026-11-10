"""Invite-only account operations. Codes are stored as hashes, never in URLs."""

import hashlib
import re
import secrets
import sqlite3
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from backend import pipeline_skeleton as pipeline
from backend.hosting import site_origin

INVITE_LIFETIME_SECONDS = 48 * 60 * 60
RESET_LIFETIME_SECONDS = 30 * 60
SIGNUP_LIFETIME_SECONDS = 30 * 60
RECOVERY_COOLDOWN_SECONDS = 5 * 60
LOGIN_LOCK_SECONDS = 15 * 60
PASSWORD_METHOD = "pbkdf2:sha256:600000"
# Public dummy hash prevents a missing email from skipping password verification.
DUMMY_HASH = generate_password_hash("unused dummy password", method=PASSWORD_METHOD)


def email_address(value):
    value = value.strip().lower()
    if len(value) > 254 or not re.fullmatch(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+", value):
        raise ValueError("Provide a valid email address")
    return value


def password_hash(password, confirmation):
    if not 12 <= len(password) <= 128 or not password.strip() or password != confirmation:
        raise ValueError("Use a password of 12–128 characters and matching confirmation")
    return generate_password_hash(password, method=PASSWORD_METHOD)


def token_digest(token):
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise ValueError("The code is invalid, expired, or already used")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def safe_next_url(value):
    """Accept only bounded same-site paths for post-login navigation."""
    if (not isinstance(value, str) or not value or len(value) > 2048 or
            not value.startswith("/") or value.startswith("//") or "\\" in value or
            "%2f" in value.casefold() or "%5c" in value.casefold() or
            any(ord(character) < 32 for character in value)):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/") or parsed.path.startswith("//"):
        return None
    return value


def delete_user_data(conn, user_id, upload_dir):
    """Delete one account and its private records, retaining shared postings."""
    account = conn.execute("""SELECT u.email,a.is_admin FROM users u
        JOIN accounts a ON a.user_id=u.id WHERE u.id=?""", (user_id,)).fetchone()
    if account is None:
        raise ValueError("This account no longer exists.")
    if account["is_admin"] and conn.execute("SELECT COUNT(*) FROM accounts WHERE is_admin=1").fetchone()[0] <= 1:
        raise ValueError("The last administrator account cannot be deleted.")

    files = [row[0] for row in conn.execute(
        "SELECT file_name FROM resume_versions WHERE user_id=? AND file_name IS NOT NULL", (user_id,))]
    email = account["email"]
    with conn:
        conn.execute("DELETE FROM recruiter_run_history WHERE run_id IN (SELECT id FROM application_runs WHERE user_id=?)", (user_id,))
        conn.execute("DELETE FROM recruiter_jobs WHERE workflow_id IN (SELECT id FROM recruiter_workflows WHERE user_id=?)", (user_id,))
        conn.execute("DELETE FROM recruiter_followups WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM worker_events WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM worker_pulses WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM application_runs WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM recruiter_workflows WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM applications WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM account_tokens WHERE user_id=? OR lower(email)=lower(?)", (user_id, email))
        conn.execute("DELETE FROM google_login_states WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM google_link_challenges WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM google_accounts WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM feedback WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM scores WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM sent_log WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM local_similarity WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM company_subscriptions WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM user_settings WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM resume_versions WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM saved_searches WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM matching_tasks WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM match_reminders WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM resume_role_titles WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM tavily_cursors WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM tavily_searches WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM accounts WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM onboarding_reminders WHERE lower(email)=lower(?)", (email,))
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))

    failures = []
    try:
        from backend.local_embeddings import CACHE_PATH
        if CACHE_PATH.is_file():
            cache = sqlite3.connect(CACHE_PATH, timeout=5)
            try:
                with cache:
                    cache.execute("DELETE FROM results WHERE user_id=?", (user_id,))
                    cache.execute("DELETE FROM embeddings WHERE owner=?", ("resume:" + str(user_id),))
            finally:
                cache.close()
    except (OSError, sqlite3.Error) as error:
        failures.append(error)

    root = Path(upload_dir).resolve()
    for file_name in files:
        if (not isinstance(file_name, str) or not file_name or Path(file_name).name != file_name or
                "/" in file_name or "\\" in file_name):
            failures.append(ValueError("A stored resume filename is invalid."))
            continue
        path = (root / file_name).resolve()
        if path.parent != root:
            failures.append(ValueError("A stored resume path is outside the upload directory."))
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            failures.append(error)
    if failures:
        raise ExceptionGroup("Account data was removed, but private file cleanup was incomplete.", failures)
    return email


def issue_invitation(conn, email, now, owner=False, self_service=False):
    """Persist an invitation before sending; callers must not retry uncertain mail."""
    email = email_address(email)
    registration_url = site_origin() + "/register"
    pipeline.validate_email_configuration()
    timestamp = int(now.timestamp())
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if owner and (conn.execute("SELECT COUNT(*) FROM accounts WHERE is_admin=1").fetchone()[0] or
                      conn.execute("SELECT COUNT(*) FROM account_tokens WHERE is_admin=1 AND consumed_at IS NULL AND expires_at>?", (timestamp,)).fetchone()[0]):
            raise ValueError("Owner setup is already complete or an owner invitation is pending")
        existing = conn.execute("SELECT id FROM users WHERE lower(email)=?", (email,)).fetchone()
        user_id = existing[0] if existing else None
        if user_id and conn.execute("SELECT 1 FROM accounts WHERE user_id=?", (user_id,)).fetchone():
            raise ValueError("An account already exists for that email")
        pending = conn.execute("SELECT 1 FROM account_tokens WHERE email=? AND purpose='invite' AND consumed_at IS NULL AND expires_at>?", (email,timestamp)).fetchone()
        if pending:
            raise ValueError("An invitation is already pending; check delivery before trying again")
        token = secrets.token_urlsafe(32)
        lifetime = SIGNUP_LIFETIME_SECONDS if self_service else INVITE_LIFETIME_SECONDS
        conn.execute("INSERT INTO account_tokens(token_hash,purpose,email,user_id,is_admin,expires_at,created_at) VALUES(?,'invite',?,?,?,?,?)",
                     (token_digest(token),email,user_id,int(owner),timestamp+lifetime,timestamp))
    try:
        if self_service:
            pipeline.deliver_account_email(email, "StudentConnect sign-up code",
                "Open " + registration_url + " and enter this code to verify your email. "
                "It expires in 30 minutes and can be used once.\n\n" + token)
        else:
            pipeline.deliver_account_email(email,"StudentConnect invitation",
                "You were invited to StudentConnect. Open " + registration_url + " and enter this code with this email address. "
                "The invitation expires in 48 hours and can be used once.\n\n" + token)
    except (OSError, RuntimeError, ValueError) as error:
        raise RuntimeError("Registration code delivery unconfirmed; check mail before requesting another") from error


def redeem_invitation(conn, form, now):
    email = email_address(form.get("email", ""))
    digest = token_digest(form.get("code", "").strip())
    name = form.get("name", "").strip()
    if not name or len(name) > 100:
        raise ValueError("Provide a name of at most 100 characters")
    hashed = password_hash(form.get("password", ""),form.get("confirmation", ""))
    timestamp = int(now.timestamp())
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        invitation = conn.execute("SELECT * FROM account_tokens WHERE token_hash=? AND email=? AND purpose='invite' AND consumed_at IS NULL AND expires_at>?",(digest,email,timestamp)).fetchone()
        if invitation is None:
            raise ValueError("The code is invalid, expired, or already used")
        user_id = invitation["user_id"]
        if user_id is None:
            user_id = conn.execute("INSERT INTO users(name,email,resume_text,preferences_json) VALUES(?,?,'','{}')", (name,email)).lastrowid
            conn.execute("INSERT INTO user_settings(user_id,subscriptions_configured) VALUES(?,0)",(user_id,))
        else:
            user = conn.execute("SELECT email,active FROM users WHERE id=?",(user_id,)).fetchone()
            if user is None or user["email"].lower()!=email or not user["active"]:
                raise ValueError("The existing profile is unavailable; contact the owner")
        conn.execute("INSERT INTO accounts(user_id,password_hash,is_admin,created_at) VALUES(?,?,?,?)", (user_id,hashed,invitation["is_admin"],now.isoformat()))
        conn.execute("UPDATE account_tokens SET consumed_at=? WHERE token_hash=?",(timestamp,digest))
    return user_id


def request_allowed(conn, purpose, now):
    """A bounded global beta limit, persisted across restarts without storing IPs."""
    timestamp = int(now.timestamp())
    maximum = 50 if purpose == "login" else 20
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM account_request_limits WHERE purpose=?",(purpose,)).fetchone()
        if row and timestamp-row["window_started_at"] < 300:
            if row["requests"] >= maximum:
                return False
            conn.execute("UPDATE account_request_limits SET requests=requests+1 WHERE purpose=?",(purpose,))
        else:
            conn.execute("INSERT INTO account_request_limits(purpose,window_started_at,requests) VALUES(?,?,1) ON CONFLICT(purpose) DO UPDATE SET window_started_at=excluded.window_started_at,requests=1",(purpose,timestamp))
    return True


def authenticate(conn, email, password, now):
    timestamp = int(now.timestamp())
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        account = conn.execute("SELECT a.*,u.active FROM accounts a JOIN users u ON u.id=a.user_id WHERE lower(u.email)=?",(email,)).fetchone()
        hashed = account["password_hash"] if account else DUMMY_HASH
        correct = check_password_hash(hashed,password)
        if account is None or not account["active"] or not account["email_verified"] or account["locked_until"]>timestamp:
            return None
        if not correct:
            failures = (account["failed_logins"] if account["locked_until"] == 0 else 0) + 1
            conn.execute("UPDATE accounts SET failed_logins=?,locked_until=? WHERE user_id=?", (failures,timestamp+LOGIN_LOCK_SECONDS if failures>=5 else 0,account["user_id"]))
            return None
        conn.execute("UPDATE accounts SET failed_logins=0,locked_until=0,last_login_at=? WHERE user_id=?",
                     (now.isoformat(),account["user_id"]))
    return dict(account)


def issue_recovery(conn, email, now):
    recovery_url = site_origin() + "/reset-password"
    timestamp = int(now.timestamp())
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        account = conn.execute("SELECT a.* FROM accounts a JOIN users u ON u.id=a.user_id WHERE lower(u.email)=? AND u.active=1 AND a.email_verified=1",(email,)).fetchone()
        if account is None or (account["recovery_requested_at"] and timestamp-account["recovery_requested_at"]<RECOVERY_COOLDOWN_SECONDS):
            return
        pipeline.validate_email_configuration()
        token = secrets.token_urlsafe(32)
        conn.execute("UPDATE account_tokens SET consumed_at=? WHERE user_id=? AND purpose='reset' AND consumed_at IS NULL",(timestamp,account["user_id"]))
        conn.execute("INSERT INTO account_tokens(token_hash,purpose,email,user_id,expires_at,created_at) VALUES(?,'reset',?,?,?,?)",(token_digest(token),email,account["user_id"],timestamp+RESET_LIFETIME_SECONDS,timestamp))
        conn.execute("UPDATE accounts SET recovery_requested_at=? WHERE user_id=?",(timestamp,account["user_id"]))
    pipeline.deliver_account_email(email,"StudentConnect password recovery",
        "Open " + recovery_url + " and enter this code. It expires in 30 minutes and works once. "
        "If you did not request this, ignore this email.\n\n"+token)


def reset_password(conn, form, now):
    email = email_address(form.get("email",""))
    digest = token_digest(form.get("code","").strip())
    hashed = password_hash(form.get("password",""),form.get("confirmation",""))
    timestamp = int(now.timestamp())
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        token = conn.execute("SELECT t.user_id FROM account_tokens t JOIN accounts a ON a.user_id=t.user_id JOIN users u ON u.id=a.user_id WHERE t.token_hash=? AND t.email=? AND t.purpose='reset' AND t.consumed_at IS NULL AND t.expires_at>? AND u.active=1 AND a.email_verified=1",(digest,email,timestamp)).fetchone()
        if token is None:
            raise ValueError("The code is invalid, expired, or already used")
        conn.execute("UPDATE accounts SET password_hash=?,session_version=session_version+1,failed_logins=0,locked_until=0 WHERE user_id=?",(hashed,token[0]))
        conn.execute("UPDATE account_tokens SET consumed_at=? WHERE user_id=? AND purpose='reset' AND consumed_at IS NULL",(timestamp,token[0]))


def register_routes(app, connection, context, now, upload_dir):
    routes = Blueprint("accounts",__name__)

    def page(mode, message=None, status=200, registration_step="request", next_url=None):
        return render_template("account.html",mode=mode,message=message,registration_step=registration_step,
                               next_url=next_url),status

    @routes.route("/login",methods=["GET","POST"])
    def login():
        next_url = safe_next_url(request.values.get("next")) or url_for("dashboard")
        if request.method=="GET":
            return page("login", next_url=next_url)
        if not request_allowed(connection(),"login",now()):
            return page("login","Too many attempts. Wait five minutes and try again.",429,next_url=next_url)
        try:
            email = email_address(request.form.get("email",""))
            password = request.form.get("password","")
            account = authenticate(connection(),email,password,now()) if 1<=len(password)<=128 else None
        except ValueError:
            account = None
        if account is None:
            return page("login","Unable to sign in. Check your credentials or wait before trying again.",400,next_url=next_url)
        session.clear()
        session.permanent=True
        session["user_id"]=account["user_id"]
        session["session_version"]=account["session_version"]
        return redirect(next_url)

    @routes.post("/logout")
    def logout():
        with connection() as conn:
            conn.execute("UPDATE accounts SET session_version=session_version+1 WHERE user_id=?",(g.user["id"],))
        session.clear()
        return redirect(url_for("accounts.login"))

    @routes.post("/account/delete")
    def delete_own_account():
        if g.account["is_admin"]:
            flash("Administrator accounts cannot be deleted from this page.")
            return redirect(url_for("settings_page"))
        if request.form.get("confirm_delete", "") != "DELETE":
            flash("Type DELETE exactly to confirm account removal.")
            return redirect(url_for("settings_page"))
        try:
            delete_user_data(connection(), g.user["id"], upload_dir)
        except ValueError as error:
            flash(str(error))
            return redirect(url_for("settings_page"))
        except ExceptionGroup as error:
            app.logger.error("Account data was removed but private file cleanup failed (%d failures)", len(error.exceptions))
            session.clear()
            flash("Your account was deleted, but some private files could not be cleared. Contact the site administrator.")
            return redirect(url_for("accounts.login"))
        session.clear()
        flash("Your account and personal data have been deleted.")
        return redirect(url_for("accounts.login"))

    @routes.route("/register",methods=["GET","POST"])
    def register():
        if request.method=="POST":
            if not request_allowed(connection(),"login",now()):
                return page("register","Too many attempts. Wait five minutes.",429)
            try:
                redeem_invitation(connection(),request.form,now())
            except (ValueError,sqlite3.IntegrityError):
                return page("register","Registration failed. Check the code, email, name and password requirements.",400,"complete")
            session.clear()
            flash("Account created and email verified. Sign in to continue.")
            return redirect(url_for("accounts.login"))
        return page("register",registration_step="complete" if request.args.get("step")=="complete" else "request")

    @routes.post("/register/code")
    def registration_code():
        if not request_allowed(connection(), "reset", now()):
            return page("register", "Too many code requests. Wait five minutes.", 429)
        try:
            pipeline.validate_email_configuration()
        except ValueError as error:
            app.logger.error("Registration code delivery unavailable (%s)", type(error).__name__)
            return page("register", "Email codes are temporarily unavailable. Please try again later.", 503)
        try:
            issue_invitation(connection(), request.form.get("email", ""), now(), self_service=True)
        except ValueError:
            # Account existence and pending invitations must not be disclosed.
            return page("register", "If this email is eligible, a sign-up code has been sent. Check your inbox before requesting another.", registration_step="complete")
        except (OSError, RuntimeError) as error:
            app.logger.error("Registration code delivery unconfirmed (%s); inspect delivery before retrying", type(error).__name__)
        return page("register", "If this email is eligible, a sign-up code has been sent. Check your inbox before requesting another.", registration_step="complete")

    @routes.route("/forgot-password",methods=["GET","POST"])
    def forgot():
        if request.method=="POST":
            if not request_allowed(connection(),"reset",now()):
                return page("forgot","Too many requests. Wait five minutes.",429)
            try:
                email=email_address(request.form.get("email",""))
            except ValueError:
                return page("forgot","If an eligible account exists, recovery instructions will be emailed.")
            try:
                issue_recovery(connection(),email,now())
            except (OSError,RuntimeError,ValueError) as error:
                app.logger.error("Account recovery mail failed (%s); inspect delivery before retrying",type(error).__name__)
            # Deliberately identical for unknown accounts and delivery failures.
            return page("forgot","If an eligible account exists, recovery instructions will be emailed.")
        return page("forgot")

    @routes.route("/reset-password",methods=["GET","POST"])
    def reset():
        if request.method=="POST":
            if not request_allowed(connection(),"reset",now()):
                return page("reset","Too many attempts. Wait five minutes.",429)
            try:
                reset_password(connection(),request.form,now())
            except ValueError:
                return page("reset","Reset failed. Check the code, email and password requirements.",400)
            session.clear()
            flash("Password changed. Sign in again; previous sessions have been revoked.")
            return redirect(url_for("accounts.login"))
        return page("reset")

    @routes.route("/accounts/invitations",methods=["GET","POST"])
    def invitations():
        if not g.account["is_admin"]:
            return "Only the owner can invite testers.",403
        data=context()
        if request.method=="POST":
            try:
                issue_invitation(connection(),request.form.get("email",""),now())
            except ValueError as error:
                return render_template("invitations.html",**dict(data,message=str(error))),400
            except (OSError,RuntimeError) as error:
                app.logger.error("Invitation mail failed (%s); pending invitation retained",type(error).__name__)
                return render_template("invitations.html",**dict(data,message="Invitation delivery was not confirmed. Check Gmail before trying again.")),503
            flash("Invitation accepted by Gmail. The tester can enter the emailed code on Register.")
            return redirect(url_for("accounts.invitations"))
        return render_template("invitations.html",**data)

    app.config.update(PERMANENT_SESSION_LIFETIME=timedelta(hours=8),SESSION_REFRESH_EACH_REQUEST=False)
    app.register_blueprint(routes)
