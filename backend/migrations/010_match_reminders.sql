CREATE TABLE match_reminders (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    profile_fingerprint TEXT NOT NULL,
    anchor_at TEXT NOT NULL,
    last_job_email_at TEXT,
    last_reminder_at TEXT,
    sent_count INTEGER NOT NULL DEFAULT 0 CHECK(sent_count BETWEEN 0 AND 2)
);
