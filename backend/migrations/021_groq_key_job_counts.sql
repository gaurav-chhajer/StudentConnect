ALTER TABLE groq_key_history ADD COLUMN job_count INTEGER NOT NULL DEFAULT 0 CHECK(job_count >= 0);
