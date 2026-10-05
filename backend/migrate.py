"""Apply additive, versioned migrations without deleting existing data."""

import hashlib
from pathlib import Path


def apply_migrations(conn):
    with conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, checksum TEXT NOT NULL)")
        conn.execute("BEGIN IMMEDIATE")
        for path in sorted((Path(__file__).parent / "migrations").glob("*.sql")):
            sql = path.read_text(encoding="utf-8")
            checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            applied = conn.execute("SELECT checksum FROM schema_migrations WHERE name=?", (path.name,)).fetchone()
            if applied:
                if applied[0] != checksum:
                    raise ValueError("An applied migration was modified: " + path.name)
                continue
            # Migration files contain simple DDL/DML, without triggers or semicolons in literals.
            for statement in sql.split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute("INSERT INTO schema_migrations(name,checksum) VALUES(?,?)", (path.name, checksum))


if __name__ == "__main__":
    from backend.pipeline_skeleton import init_db
    init_db()
