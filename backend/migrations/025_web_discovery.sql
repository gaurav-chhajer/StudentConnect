ALTER TABLE companies ADD COLUMN discovery_only INTEGER NOT NULL DEFAULT 0 CHECK (discovery_only IN (0,1));
CREATE TABLE tavily_budget (
    id INTEGER PRIMARY KEY CHECK (id=1),
    day TEXT NOT NULL,
    month TEXT NOT NULL,
    daily_used INTEGER NOT NULL CHECK (daily_used>=0),
    monthly_used INTEGER NOT NULL CHECK (monthly_used>=0),
    last_user INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE tavily_cursors (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    position INTEGER NOT NULL DEFAULT 0 CHECK (position>=0)
);
CREATE TABLE tavily_searches (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    fingerprint TEXT NOT NULL,
    title TEXT NOT NULL,
    platform TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'reserved' CHECK (status IN ('reserved','complete','failed')),
    verified INTEGER NOT NULL DEFAULT 0,
    rejected INTEGER NOT NULL DEFAULT 0,
    saved INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE discovery_job_signatures (
    signature TEXT PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id)
);
