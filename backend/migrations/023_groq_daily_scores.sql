CREATE TABLE groq_daily_scores (
    day_ist TEXT NOT NULL CHECK(length(day_ist)=10),
    key_slot INTEGER NOT NULL CHECK(key_slot BETWEEN 1 AND 5),
    job_count INTEGER NOT NULL CHECK(job_count >= 0),
    PRIMARY KEY(day_ist,key_slot)
);
