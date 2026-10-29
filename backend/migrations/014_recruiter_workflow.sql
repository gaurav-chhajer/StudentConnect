CREATE TABLE recruiter_workflows (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    resume_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('analyzed','searching','awaiting_approval','approved','materials_ready')),
    analysis_json TEXT NOT NULL,
    coverage_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE recruiter_jobs (
    workflow_id INTEGER NOT NULL REFERENCES recruiter_workflows(id),
    number INTEGER NOT NULL,
    job_id INTEGER REFERENCES jobs(id),
    posting_json TEXT NOT NULL,
    facts_json TEXT NOT NULL,
    evaluation_json TEXT NOT NULL,
    selected INTEGER NOT NULL DEFAULT 0 CHECK(selected IN (0,1)),
    review_json TEXT,
    materials_json TEXT,
    outreach_json TEXT,
    PRIMARY KEY(workflow_id,number),
    UNIQUE(workflow_id,job_id)
);
CREATE INDEX recruiter_jobs_job ON recruiter_jobs(job_id,selected);
CREATE TABLE recruiter_followups (
    application_id INTEGER PRIMARY KEY REFERENCES applications(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    due_date TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX recruiter_followups_due ON recruiter_followups(user_id,due_date);
CREATE TABLE recruiter_run_history (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES application_runs(id),
    snapshot_json TEXT NOT NULL,
    document BLOB,
    archived_at TEXT NOT NULL
);
