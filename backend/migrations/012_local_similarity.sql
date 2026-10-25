CREATE TABLE local_similarity (
    user_id INTEGER NOT NULL REFERENCES users(id),
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    resume_hash TEXT NOT NULL,
    job_hash TEXT NOT NULL,
    preferences_hash TEXT NOT NULL,
    model_revision TEXT NOT NULL,
    scoring_version TEXT NOT NULL,
    cosine REAL NOT NULL CHECK(cosine BETWEEN -1 AND 1),
    skill_overlap REAL NOT NULL CHECK(skill_overlap BETWEEN 0 AND 1),
    rank_score REAL NOT NULL CHECK(rank_score BETWEEN 0 AND 100),
    matched_skills TEXT NOT NULL,
    jd_skills_not_mentioned TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(user_id,job_id)
);
CREATE INDEX local_similarity_ranking ON local_similarity(user_id,rank_score DESC,job_id);
