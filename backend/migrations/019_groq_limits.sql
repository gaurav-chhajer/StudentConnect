CREATE TABLE groq_limits (
 key_slot INTEGER PRIMARY KEY CHECK(key_slot BETWEEN 1 AND 3),
 model TEXT NOT NULL,
 metrics_json TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
