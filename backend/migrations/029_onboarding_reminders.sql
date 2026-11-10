CREATE TABLE onboarding_reminders (
    email TEXT PRIMARY KEY COLLATE NOCASE,
    attempted_at INTEGER NOT NULL,
    sent_at INTEGER
);
