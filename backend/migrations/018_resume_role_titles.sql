CREATE TABLE resume_role_titles (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    resume_hash TEXT NOT NULL,
    version TEXT NOT NULL,
    model TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
