CREATE TABLE application_runs (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    input_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('preparing','needs_review','ready','submitting','applied','unknown','failed','skipped')),
    original_score INTEGER NOT NULL CHECK(original_score BETWEEN 0 AND 100),
    final_score INTEGER CHECK(final_score BETWEEN 0 AND 100),
    target_score INTEGER NOT NULL CHECK(target_score IN (80,90)),
    resume_text TEXT,
    document BLOB,
    document_hash TEXT,
    evaluation_json TEXT,
    detail TEXT NOT NULL DEFAULT '',
    confirmation TEXT,
    notion_page_id TEXT,
    notion_state TEXT NOT NULL DEFAULT 'local' CHECK(notion_state IN ('local','pending','synced','unknown')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id,job_id)
);
CREATE INDEX application_runs_state ON application_runs(user_id,state,id);
