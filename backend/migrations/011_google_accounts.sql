CREATE TABLE google_accounts (
    subject TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL UNIQUE REFERENCES accounts(user_id)
);

CREATE TABLE google_login_states (
    state_hash TEXT PRIMARY KEY,
    browser_hash TEXT NOT NULL,
    nonce TEXT NOT NULL,
    code_verifier TEXT NOT NULL,
    user_id INTEGER REFERENCES accounts(user_id),
    session_version INTEGER,
    expires_at INTEGER NOT NULL
);
