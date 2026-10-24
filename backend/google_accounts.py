"""Google OpenID Connect login with browser-bound, single-use server-side state."""

import os
import re
import sqlite3

from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.flask_client import OAuth
from flask import Blueprint, g, redirect, render_template, request, session, url_for
from joserfc.errors import JoseError
from requests.exceptions import RequestException

from backend import accounts
from backend.hosting import site_origin

STATE_SECONDS = 300
COOKIE_NAME = "sc_google_login"
CALLBACK_PATH = "/auth/google/callback"


def google_account(conn, claims, now, linking_user=None, linking_version=None):
    """Use a validated Google subject; linking an existing profile needs its session."""
    if not isinstance(claims, dict) or claims.get("email_verified") is not True:
        raise ValueError("Google must verify your email address before sign-in.")
    subject, email, name = claims.get("sub"), claims.get("email"), claims.get("name")
    if not isinstance(subject, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", subject):
        raise ValueError("Google returned an invalid account identifier.")
    if not isinstance(email, str):
        raise ValueError("Google did not provide an email address.")
    email = accounts.email_address(email)
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
        raise ValueError("Google must provide a name of at most 100 characters.")
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        linked = conn.execute("SELECT user_id FROM google_accounts WHERE subject=?", (subject,)).fetchone()
        if linked:
            account = conn.execute("SELECT a.*,u.email,u.active FROM accounts a JOIN users u ON u.id=a.user_id WHERE a.user_id=?", (linked[0],)).fetchone()
            if linking_user is not None and linked[0] != linking_user:
                raise ValueError("This Google account is already linked to another profile.")
        elif linking_user is not None:
            account = conn.execute("SELECT a.*,u.email,u.active FROM accounts a JOIN users u ON u.id=a.user_id AND a.session_version=?", (linking_user, linking_version)).fetchone()
        else:
            if conn.execute("SELECT 1 FROM users WHERE lower(email)=?", (email,)).fetchone():
                raise ValueError("This email already has a profile. Sign in with your password first, then open Sign in and choose Continue with Google to link it.")
            user_id = conn.execute("INSERT INTO users(name,email,resume_text,preferences_json) VALUES(?,?,'','{}')", (name.strip(), email)).lastrowid
            conn.execute("INSERT INTO user_settings(user_id,subscriptions_configured) VALUES(?,1)", (user_id,))
            # A Google account has no usable password until its owner requests recovery.
            random_password = accounts.secrets.token_urlsafe(48)
            hashed = accounts.password_hash(random_password, random_password)
            conn.execute("INSERT INTO accounts(user_id,password_hash,created_at) VALUES(?,?,?)", (user_id, hashed, now.isoformat()))
            account = conn.execute("SELECT a.*,u.email,u.active FROM accounts a JOIN users u ON u.id=a.user_id WHERE a.user_id=?", (user_id,)).fetchone()
        if (account is None or not account["active"] or not account["email_verified"] or
                account["email"].lower() != email or account["locked_until"] > int(now.timestamp()) or
                (linking_user is not None and account["session_version"] != linking_version)):
            raise ValueError("This profile is unavailable for Google sign-in. Use your existing sign-in method or contact the owner.")
        if not linked:
            conn.execute("INSERT INTO google_accounts(subject,user_id) VALUES(?,?)", (subject, account["user_id"]))
    return dict(account)


def register_routes(app, connection, now):
    client_id = os.environ.get("STUDENTCONNECT_GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("STUDENTCONNECT_GOOGLE_CLIENT_SECRET", "")
    if bool(client_id) != bool(client_secret):
        raise ValueError("Configure both Google client ID and client secret, or leave both empty")
    if any(value != value.strip() or len(value) > 1024 or any(ord(c) < 33 for c in value)
           for value in (client_id, client_secret) if value):
        raise ValueError("Google credentials contain invalid whitespace or exceed length limits")
    app.config["GOOGLE_LOGIN_ENABLED"] = bool(client_id)
    oauth = OAuth(app)
    google = oauth.register(
        "google", client_id=client_id, client_secret=client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile", "code_challenge_method": "S256", "timeout": 10},
    )
    routes = Blueprint("google_accounts", __name__)

    def failure(message, status=400):
        response = app.make_response((render_template("account.html", mode="login", message=message), status))
        response.delete_cookie(COOKIE_NAME, path=CALLBACK_PATH, secure=app.config["SESSION_COOKIE_SECURE"], httponly=True, samesite="Lax")
        return response

    @routes.post("/auth/google")
    def login():
        if not app.config["GOOGLE_LOGIN_ENABLED"]:
            return failure("Google sign-in is not configured yet.", 503)
        if not accounts.request_allowed(connection(), "login", now()):
            return failure("Too many attempts. Wait five minutes and try again.", 429)
        state, browser = accounts.secrets.token_urlsafe(32), accounts.secrets.token_urlsafe(32)
        try:
            authorization = google.create_authorization_url(site_origin() + CALLBACK_PATH, state=state, prompt="select_account")
        except (OAuthError, JoseError, RequestException, ValueError) as error:
            app.logger.error("Google authorization unavailable (%s)", type(error).__name__)
            return failure("Google sign-in is temporarily unavailable. Try again later.", 503)
        timestamp = int(now().timestamp())
        with connection() as conn:
            conn.execute("DELETE FROM google_login_states WHERE expires_at<=?", (timestamp,))
            conn.execute("INSERT INTO google_login_states(state_hash,browser_hash,nonce,code_verifier,user_id,session_version,expires_at) VALUES(?,?,?,?,?,?,?)",
                         (accounts.token_digest(state), accounts.token_digest(browser), authorization["nonce"], authorization["code_verifier"],
                          g.account["user_id"] if g.account else None, g.account["session_version"] if g.account else None, timestamp + STATE_SECONDS))
        response = redirect(authorization["url"])
        response.set_cookie(COOKIE_NAME, browser, max_age=STATE_SECONDS, path=CALLBACK_PATH,
                            secure=app.config["SESSION_COOKIE_SECURE"], httponly=True, samesite="Lax")
        return response

    @routes.get(CALLBACK_PATH)
    def callback():
        if not app.config["GOOGLE_LOGIN_ENABLED"]:
            return failure("Google sign-in is not configured yet.", 503)
        try:
            state_hash = accounts.token_digest(request.args.get("state", ""))
            browser_hash = accounts.token_digest(request.cookies.get(COOKIE_NAME, ""))
        except ValueError:
            return failure("Google sign-in expired. Start again from Sign in.")
        with connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            pending = conn.execute("SELECT * FROM google_login_states WHERE state_hash=? AND browser_hash=? AND expires_at>?", (state_hash, browser_hash, int(now().timestamp()))).fetchone()
            if pending:
                conn.execute("DELETE FROM google_login_states WHERE state_hash=?", (state_hash,))
        if pending is None:
            return failure("Google sign-in expired. Start again from Sign in.")
        if request.args.get("error"):
            return failure("Google sign-in was cancelled. You can try again.")
        code = request.args.get("code", "")
        if not code or len(code) > 4096:
            return failure("Google sign-in did not return a valid authorization code.")
        try:
            token = google.fetch_access_token(redirect_uri=site_origin() + CALLBACK_PATH, code=code, code_verifier=pending["code_verifier"])
            claims = google.parse_id_token(token, nonce=pending["nonce"], leeway=30)
        except (OAuthError, JoseError, RequestException, ValueError) as error:
            app.logger.error("Google sign-in verification failed (%s)", type(error).__name__)
            return failure("Google sign-in could not be verified. Start again or try later.")
        try:
            account = google_account(connection(), claims, now(), pending["user_id"], pending["session_version"])
        except ValueError as error:
            return failure(str(error))
        except sqlite3.IntegrityError:
            return failure("This profile is already linked. Use your existing sign-in method.")
        session.clear()
        session.permanent = True
        session["user_id"], session["session_version"] = account["user_id"], account["session_version"]
        response = redirect(url_for("dashboard"))
        response.delete_cookie(COOKIE_NAME, path=CALLBACK_PATH, secure=app.config["SESSION_COOKIE_SECURE"], httponly=True, samesite="Lax")
        return response

    app.register_blueprint(routes)
