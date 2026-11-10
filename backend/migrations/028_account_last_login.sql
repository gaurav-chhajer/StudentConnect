ALTER TABLE accounts ADD COLUMN last_login_at TEXT;
CREATE INDEX accounts_last_login ON accounts(last_login_at);
