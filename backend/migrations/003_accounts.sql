CREATE UNIQUE INDEX users_email_case_insensitive ON users(lower(email));

CREATE TABLE accounts (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0 CHECK(is_admin IN (0,1)),
    email_verified INTEGER NOT NULL DEFAULT 1 CHECK(email_verified IN (0,1)),
    session_version INTEGER NOT NULL DEFAULT 0,
    failed_logins INTEGER NOT NULL DEFAULT 0,
    locked_until INTEGER NOT NULL DEFAULT 0,
    recovery_requested_at INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE account_tokens (
    token_hash TEXT PRIMARY KEY,
    purpose TEXT NOT NULL CHECK(purpose IN ('invite','reset')),
    email TEXT NOT NULL,
    user_id INTEGER REFERENCES users(id),
    is_admin INTEGER NOT NULL DEFAULT 0 CHECK(is_admin IN (0,1)),
    expires_at INTEGER NOT NULL,
    consumed_at INTEGER,
    created_at INTEGER NOT NULL
);
CREATE INDEX account_tokens_email_purpose ON account_tokens(email,purpose,created_at);

CREATE TABLE account_request_limits (
    purpose TEXT PRIMARY KEY CHECK(purpose IN ('login','reset')),
    window_started_at INTEGER NOT NULL,
    requests INTEGER NOT NULL
);
