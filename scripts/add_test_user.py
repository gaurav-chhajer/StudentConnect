"""
Adds (or updates) one test user in the local jobs.db so you have someone
to score jobs against. Edit RESUME_TEXT and PREFERENCES below to match
your own resume before running.

Usage:
    python -m scripts.add_test_user
"""

import json
import sqlite3
from datetime import datetime, timezone
from backend import pipeline_skeleton as p

EMAIL = "test@example.com"
NAME = "Test User"

# Paste your actual resume text here (plain text is fine - no need to
# format it nicely, the model just reads it).
RESUME_TEXT = """
PASTE RESUME TEXT HERE
"""

# roles/locations are substring-matched against job title/department/location.
# Leave a list empty (e.g. "locations": []) to skip filtering on that axis.
PREFERENCES = {
    "roles": ["SDE", "Software Engineer", "Backend", "Full Stack"],
    "locations": ["Bangalore", "Remote"],
    "min_stipend": None,  # e.g. 20000 - leave None if you don't want this filter active yet
}

if __name__ == "__main__":
    p.init_db()
    conn = sqlite3.connect(p.DB_PATH)
    previous = conn.execute("SELECT id,resume_text,preferences_json FROM users WHERE email=?", (EMAIL,)).fetchone()
    conn.execute(
        """
        INSERT INTO users (name, email, resume_text, preferences_json, active)
        VALUES (?, ?, ?, ?, 1)
        ON CONFLICT(email) DO UPDATE SET
            name=excluded.name, resume_text=excluded.resume_text,
            preferences_json=excluded.preferences_json, active=1
        """,
        (NAME, EMAIL, RESUME_TEXT.strip(), json.dumps(PREFERENCES)),
    )
    if not previous or previous[1] != RESUME_TEXT.strip():
        timestamp = datetime.now(timezone.utc).isoformat()
        user_id = conn.execute("SELECT id FROM users WHERE email=?", (EMAIL,)).fetchone()[0]
        with conn:
            conn.execute("UPDATE users SET resume_updated_at=? WHERE id=?", (timestamp, user_id))
            conn.execute("INSERT INTO resume_versions(user_id,resume_text,source_name,created_at) VALUES(?,?,?,?)",
                         (user_id, RESUME_TEXT.strip(), "Test-user script", timestamp))
            conn.execute("UPDATE scores SET is_stale=1 WHERE user_id=?", (user_id,))
    elif previous[2] != json.dumps(PREFERENCES):
        with conn:
            conn.execute("UPDATE scores SET is_stale=1 WHERE user_id=?", (previous[0],))
    conn.commit()
    conn.close()
    print(f"User '{EMAIL}' added/updated.")
