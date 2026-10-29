CREATE TABLE worker_pulses (
    user_id INTEGER NOT NULL REFERENCES users(id),
    worker TEXT NOT NULL,
    workflow_id INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL,
    code TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    next_check_at TEXT,
    PRIMARY KEY(user_id,worker,workflow_id)
);
CREATE TABLE worker_events (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    worker TEXT NOT NULL,
    workflow_id INTEGER REFERENCES recruiter_workflows(id),
    job_id INTEGER REFERENCES jobs(id),
    number INTEGER,
    code TEXT NOT NULL,
    value REAL,
    created_at TEXT NOT NULL
);
CREATE INDEX worker_events_owner ON worker_events(user_id,id DESC);
CREATE INDEX worker_events_workflow ON worker_events(user_id,workflow_id,id DESC);
