"""Tester feedback: a signed-in form plus an owner-only review page."""

import hashlib
import hmac
from datetime import timedelta

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, session, url_for

from backend import store

MAX_TEXT = 2000
DAILY_LIMIT = 10
ANSWERS = ("yes", "maybe", "no")


def save_feedback(conn, user_id, form, now, anonymous_key=None):
    """Validate and store one submission; raises ValueError with a user-facing reason."""
    try:
        accuracy = store.integer(form.get("accuracy", ""), 1, 5)
        ease = store.integer(form.get("ease", ""), 1, 5)
    except ValueError:
        raise ValueError("Choose a rating from 1 to 5 for both questions") from None
    would_use = form.get("would_use", "")
    issues = form.get("issues", "").strip()
    feature_request = form.get("feature_request", "").strip()
    if would_use not in ANSWERS:
        raise ValueError("Choose Yes, Maybe or No for the last question")
    if len(issues) > MAX_TEXT or len(feature_request) > MAX_TEXT:
        raise ValueError(f"Comments can be at most {MAX_TEXT} characters")
    cutoff = (now - timedelta(days=1)).isoformat()
    if user_id is None and (not isinstance(anonymous_key, str) or not anonymous_key):
        raise ValueError("Reload the form and try again")
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        recent = conn.execute("""SELECT COUNT(*) FROM feedback WHERE created_at>=? AND
            ((? IS NOT NULL AND user_id=?) OR (? IS NULL AND anonymous_key=?))""",
                              (cutoff, user_id, user_id, user_id, anonymous_key)).fetchone()[0]
        if recent >= DAILY_LIMIT:
            raise ValueError("You have reached today's feedback limit. Please try again tomorrow")
        conn.execute("""INSERT INTO feedback(user_id,anonymous_key,accuracy,ease,would_use,issues,feature_request,created_at)
            VALUES(?,?,?,?,?,?,?,?)""", (user_id, anonymous_key, accuracy, ease, would_use, issues, feature_request, now.isoformat()))


def register_routes(app, connection, context, now):
    routes = Blueprint("feedback", __name__)

    @routes.route("/feedback", methods=["GET", "POST"])
    def submit():
        data = context()
        if request.method == "POST":
            try:
                anonymous_key = None
                user_id = g.user["id"] if g.user else None
                if user_id is None:
                    token = session.setdefault("feedback_token", __import__("secrets").token_urlsafe(32))
                    anonymous_key = hmac.new(str(current_app.secret_key).encode(), token.encode(), hashlib.sha256).hexdigest()
                save_feedback(connection(), user_id, request.form, now(), anonymous_key)
            except ValueError as error:
                return render_template("feedback.html", **dict(data, message=str(error))), 400
            flash("Thank you. Your feedback was saved.")
            return redirect(url_for("feedback.submit"))
        return render_template("feedback.html", **data)

    @routes.get("/admin/feedback")
    def review():
        if not g.account["is_admin"]:
            return "Only the owner can read tester feedback.", 403
        conn = connection()
        rows = conn.execute("""SELECT f.*,COALESCE(u.name,'Anonymous tester') name,COALESCE(u.email,'') email
            FROM feedback f LEFT JOIN users u ON u.id=f.user_id
            ORDER BY f.created_at DESC,f.id DESC LIMIT 200""").fetchall()
        totals = conn.execute("""SELECT COUNT(*) count,ROUND(AVG(accuracy),2) accuracy,ROUND(AVG(ease),2) ease,
            COALESCE(SUM(would_use='yes'),0) yes,COALESCE(SUM(would_use='maybe'),0) maybe,
            COALESCE(SUM(would_use='no'),0) no FROM feedback""").fetchone()
        score_rows = conn.execute("""SELECT sf.rating,sf.created_at,sf.score_signature,j.title,c.name company_name
            FROM score_feedback sf JOIN jobs j ON j.id=sf.job_id
            JOIN companies c ON c.id=j.company_id
            ORDER BY sf.created_at DESC,sf.id DESC LIMIT 200""").fetchall()
        score_totals = conn.execute("""SELECT COUNT(*) count,COALESCE(SUM(rating=1),0) helpful,
            COALESCE(SUM(rating=-1),0) inaccurate FROM score_feedback""").fetchone()
        return render_template("feedback_admin.html", **dict(context(), rows=rows, totals=totals,
                                                                  score_rows=score_rows, score_totals=score_totals))

    app.register_blueprint(routes)
