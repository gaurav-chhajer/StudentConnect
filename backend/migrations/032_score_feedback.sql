CREATE TABLE score_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    score_signature TEXT NOT NULL CHECK(length(score_signature)=64),
    rating INTEGER NOT NULL CHECK(rating IN (-1,1)),
    created_at TEXT NOT NULL,
    UNIQUE(user_id,job_id,score_signature)
);
CREATE INDEX score_feedback_job_created ON score_feedback(job_id,created_at);
