"""Score-specific user feedback for deterministic and legacy saved matches."""

import hashlib
import json
import re

from flask import Blueprint, flash, g, redirect, request, url_for


def score_signature(row):
    """Return a stable opaque signature for the currently displayed saved score."""
    payload = {key: row[key] for key in (
        "id", "fit_score", "reasoning", "matched_skills", "missing_requirements",
        "evaluation_json", "deterministic_json", "scored_at")}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def register_routes(app, connection, now):
    routes = Blueprint("score_feedback", __name__)

    @routes.post("/jobs/score-feedback")
    def submit():
        try:
            job_id = int(request.form.get("job_id", ""))
            signature = request.form.get("score_signature", "")
            rating = int(request.form.get("rating", ""))
        except (TypeError, ValueError):
            flash("That score feedback could not be saved. Reload the match and try again.")
            return redirect(url_for("jobs"))
        if (not 1 <= job_id <= 2147483647 or not re.fullmatch(r"[0-9a-f]{64}", signature) or
                rating not in (-1, 1)):
            flash("That score feedback could not be saved. Reload the match and try again.")
            return redirect(url_for("jobs"))
        conn = connection()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("""SELECT s.id,s.fit_score,s.reasoning,s.matched_skills,s.missing_requirements,
                    s.evaluation_json,s.deterministic_json,s.scored_at
                FROM scores s JOIN jobs j ON j.id=s.job_id
                WHERE s.user_id=? AND s.job_id=? AND s.is_stale=0 AND j.is_active=1""",
                (g.user["id"], job_id)).fetchone()
            if row is None or score_signature(row) != signature:
                flash("This score has changed. Review the updated match before rating it.")
                return redirect(url_for("jobs", job=job_id))
            conn.execute("""INSERT INTO score_feedback(user_id,job_id,score_signature,rating,created_at,fit_score)
                VALUES(?,?,?,?,?,?) ON CONFLICT(user_id,job_id,score_signature)
                DO UPDATE SET rating=excluded.rating,created_at=excluded.created_at,fit_score=excluded.fit_score""",
                (g.user["id"], job_id, signature, rating, now().isoformat(), row['fit_score']))
        flash("Thanks. Your score feedback was saved.")
        return redirect(url_for("jobs", job=job_id))

    app.register_blueprint(routes)
