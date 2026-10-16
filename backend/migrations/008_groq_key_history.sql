CREATE TABLE groq_key_history (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL CHECK(length(run_id)=32),
    key_slot INTEGER NOT NULL CHECK(key_slot BETWEEN 1 AND 3),
    state TEXT NOT NULL CHECK(state IN ('not_configured','not_attempted','attempting','succeeded','rate_limited','failed')),
    outcome TEXT NOT NULL CHECK(outcome IN ('running','complete','deferred')),
    revision INTEGER NOT NULL CHECK(revision BETWEEN 0 AND 1000),
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id,key_slot)
);
CREATE INDEX groq_key_history_runs ON groq_key_history(key_slot,id);
