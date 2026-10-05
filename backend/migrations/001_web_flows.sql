ALTER TABLE scores ADD COLUMN is_stale INTEGER NOT NULL DEFAULT 0 CHECK (is_stale IN (0,1));

CREATE TABLE user_settings (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    university TEXT NOT NULL DEFAULT '',
    graduation_year INTEGER,
    cadence TEXT NOT NULL DEFAULT 'daily' CHECK (cadence IN ('daily','weekly','off')),
    send_time TEXT NOT NULL DEFAULT '08:00',
    timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata',
    min_score INTEGER NOT NULL DEFAULT 60 CHECK (min_score BETWEEN 0 AND 100),
    include_gaps INTEGER NOT NULL DEFAULT 1 CHECK (include_gaps IN (0,1)),
    subscriptions_configured INTEGER NOT NULL DEFAULT 0 CHECK (subscriptions_configured IN (0,1))
);

CREATE TABLE company_subscriptions (
    user_id INTEGER NOT NULL REFERENCES users(id),
    company_id INTEGER NOT NULL REFERENCES companies(id),
    subscribed_at TEXT NOT NULL,
    PRIMARY KEY (user_id,company_id)
);

CREATE TABLE resume_versions (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    resume_text TEXT NOT NULL,
    source_name TEXT NOT NULL,
    file_name TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE applications (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    job_id INTEGER REFERENCES jobs(id),
    title TEXT NOT NULL,
    company_name TEXT NOT NULL,
    url TEXT,
    status TEXT NOT NULL CHECK (status IN ('saved','applied','interview','offer','rejected')),
    notes TEXT NOT NULL DEFAULT '',
    applied_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id,job_id)
);

CREATE TABLE saved_searches (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    filters_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id,name)
);

CREATE INDEX applications_user_status ON applications(user_id,status);
CREATE INDEX resume_versions_user_date ON resume_versions(user_id,created_at);
CREATE INDEX scores_user_stale ON scores(user_id,is_stale);

INSERT INTO user_settings(user_id) SELECT id FROM users;
INSERT INTO resume_versions(user_id,resume_text,source_name,created_at)
SELECT id,resume_text,'Existing profile',COALESCE(resume_updated_at,strftime('%Y-%m-%dT%H:%M:%f+00:00','now'))
FROM users WHERE length(trim(resume_text))>0;
