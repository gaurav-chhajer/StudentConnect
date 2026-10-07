CREATE TABLE matching_tasks (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
    reason TEXT NOT NULL CHECK(reason IN ('companies','preferences','resume','postings','backlog')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','complete','failed')),
    last_error TEXT CHECK(last_error IN ('fetch_failed','scoring_failed')),
    updated_at TEXT NOT NULL
);
CREATE INDEX matching_tasks_status_updated ON matching_tasks(status,updated_at);
INSERT INTO matching_tasks(user_id,reason,updated_at)
    SELECT u.id,'backlog',strftime('%Y-%m-%dT%H:%M:%SZ','now') FROM users u
    JOIN accounts a ON a.user_id=u.id WHERE u.active=1 AND a.email_verified=1;
