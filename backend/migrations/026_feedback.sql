CREATE TABLE feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    accuracy INTEGER NOT NULL CHECK(accuracy BETWEEN 1 AND 5),
    ease INTEGER NOT NULL CHECK(ease BETWEEN 1 AND 5),
    would_use TEXT NOT NULL CHECK(would_use IN ('yes','maybe','no')),
    issues TEXT NOT NULL DEFAULT '',
    feature_request TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX feedback_user_created ON feedback(user_id,created_at);
CREATE INDEX feedback_created ON feedback(created_at);
