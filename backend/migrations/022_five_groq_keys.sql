CREATE TABLE groq_key_history_five (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL CHECK(length(run_id)=32),
    key_slot INTEGER NOT NULL CHECK(key_slot BETWEEN 1 AND 5),
    state TEXT NOT NULL CHECK(state IN ('not_configured','not_attempted','attempting','succeeded','rate_limited','failed')),
    outcome TEXT NOT NULL CHECK(outcome IN ('running','complete','deferred')),
    revision INTEGER NOT NULL CHECK(revision BETWEEN 0 AND 1000),
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    job_count INTEGER NOT NULL DEFAULT 0 CHECK(job_count >= 0),
    UNIQUE(run_id,key_slot)
);
INSERT INTO groq_key_history_five
    (id,run_id,key_slot,state,outcome,revision,started_at,updated_at,job_count)
    SELECT id,run_id,key_slot,state,outcome,revision,started_at,updated_at,job_count FROM groq_key_history;
DROP TABLE groq_key_history;
ALTER TABLE groq_key_history_five RENAME TO groq_key_history;
CREATE INDEX groq_key_history_runs ON groq_key_history(key_slot,id);

CREATE TABLE groq_limits_five (
    key_slot INTEGER PRIMARY KEY CHECK(key_slot BETWEEN 1 AND 5),
    model TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT INTO groq_limits_five (key_slot,model,metrics_json,updated_at)
    SELECT key_slot,model,metrics_json,updated_at FROM groq_limits;
DROP TABLE groq_limits;
ALTER TABLE groq_limits_five RENAME TO groq_limits;
