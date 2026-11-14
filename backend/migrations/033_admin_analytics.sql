-- Preserve application IDs and references while extending the allowed stages.
CREATE TABLE applications_next (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    job_id INTEGER REFERENCES jobs(id),
    title TEXT NOT NULL,
    company_name TEXT NOT NULL,
    url TEXT,
    status TEXT NOT NULL CHECK(status IN ('saved','applied','screen','assessment','interview','offer','rejected','no_reply')),
    notes TEXT NOT NULL DEFAULT '',
    applied_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id,job_id)
);
INSERT INTO applications_next SELECT * FROM applications;
-- Rebuild the referencing table alongside its parent without disabling checks.
CREATE TABLE recruiter_followups_next (
    application_id INTEGER PRIMARY KEY REFERENCES applications_next(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    due_date TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT INTO recruiter_followups_next SELECT * FROM recruiter_followups;
DROP TABLE recruiter_followups;
DROP TABLE applications;
ALTER TABLE applications_next RENAME TO applications;
ALTER TABLE recruiter_followups_next RENAME TO recruiter_followups;
CREATE INDEX recruiter_followups_due ON recruiter_followups(user_id,due_date);
CREATE INDEX applications_user_status ON applications(user_id,status);
CREATE TABLE application_events (
    id INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK(status IN ('saved','applied','screen','assessment','interview','offer','rejected','no_reply')),
    at TEXT NOT NULL,
    date_known INTEGER NOT NULL DEFAULT 1 CHECK(date_known IN (0,1))
);
CREATE INDEX application_events_application ON application_events(application_id,id);
-- Current legacy status is known, but its transition date is not.
INSERT INTO application_events(application_id,status,at,date_known)
SELECT id,status,updated_at,0 FROM applications;
CREATE TRIGGER application_event_insert AFTER INSERT ON applications BEGIN
    INSERT INTO application_events(application_id,status,at) VALUES(NEW.id,NEW.status,NEW.updated_at);
END;
CREATE TRIGGER application_event_update AFTER UPDATE OF status ON applications
WHEN OLD.status<>NEW.status BEGIN
    INSERT INTO application_events(application_id,status,at) VALUES(NEW.id,NEW.status,NEW.updated_at);
END;
CREATE TABLE strong_match_firsts (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    match_first_at TEXT NOT NULL,
    historical INTEGER NOT NULL DEFAULT 0 CHECK(historical IN (0,1)),
    PRIMARY KEY(user_id,job_id)
);
INSERT INTO strong_match_firsts(user_id,job_id,match_first_at,historical)
SELECT user_id,job_id,scored_at,1 FROM scores WHERE fit_score>=80;
CREATE INDEX strong_match_week ON strong_match_firsts(user_id,match_first_at);
CREATE TRIGGER strong_match_insert AFTER INSERT ON scores WHEN NEW.fit_score>=80 BEGIN
    INSERT INTO strong_match_firsts(user_id,job_id,match_first_at)
    SELECT NEW.user_id,NEW.job_id,NEW.scored_at WHERE NOT EXISTS(
        SELECT 1 FROM strong_match_firsts WHERE user_id=NEW.user_id AND job_id=NEW.job_id);
END;
CREATE TRIGGER strong_match_update AFTER UPDATE OF fit_score,scored_at ON scores WHEN NEW.fit_score>=80 BEGIN
    INSERT INTO strong_match_firsts(user_id,job_id,match_first_at)
    SELECT NEW.user_id,NEW.job_id,NEW.scored_at WHERE NOT EXISTS(
        SELECT 1 FROM strong_match_firsts WHERE user_id=NEW.user_id AND job_id=NEW.job_id);
END;
CREATE TABLE admin_audit_log (
    id INTEGER PRIMARY KEY,
    actor_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id INTEGER,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX admin_audit_created ON admin_audit_log(id DESC);
CREATE TABLE account_verifications (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    requested_at INTEGER NOT NULL
);
ALTER TABLE score_feedback ADD COLUMN fit_score INTEGER CHECK(fit_score BETWEEN 0 AND 100);
ALTER TABLE jobs ADD COLUMN admin_closed INTEGER NOT NULL DEFAULT 0 CHECK(admin_closed IN (0,1));
