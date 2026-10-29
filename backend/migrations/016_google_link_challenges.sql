CREATE TABLE google_link_challenges (
    browser_hash TEXT PRIMARY KEY,
    code_hash TEXT NOT NULL,
    subject TEXT NOT NULL,
    email TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),
    expires_at INTEGER NOT NULL
);
