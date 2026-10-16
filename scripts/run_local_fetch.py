"""
Run this locally (not in the sandbox - it needs real network access to
boards-api.greenhouse.io, api.lever.co, api.ashbyhq.com).

Usage:
    python -m scripts.run_local_fetch

Uses backend.pipeline_skeleton and verified board CSVs in data/boards/.
"""

import sqlite3
import csv
from pathlib import Path
from backend import pipeline_skeleton as p
from backend.career_sources import SOURCES, UNVERIFIED_SOURCES

CSV_FILES = [Path(__file__).resolve().parents[1] / "data" / "boards" / name
             for name in ("verified_boards_1.csv", "verified_boards_batch2.csv")]


def seed_companies(conn):
    inserted = 0
    for fname in CSV_FILES:
        with open(fname, newline="") as f:
            for row in csv.DictReader(f):
                cur = conn.execute(
                    "INSERT OR IGNORE INTO companies (name, ats_type, board_slug, sector) VALUES (?, ?, ?, ?)",
                    (row["company"], row["ats"], row["slug"], None),
                )
                inserted += cur.rowcount
    # Keep the existing company ID, subscriptions and job history when moving
    # Postman from its retired Greenhouse board to the official Workday feed.
    conn.execute("""UPDATE companies SET career_source='postman'
        WHERE ats_type='greenhouse' AND board_slug='postman' AND career_source IS NULL""")
    for key, (name, kind, origin) in SOURCES.items():
        # Legacy ATS columns remain required by existing databases. They are
        # ignored whenever career_source is present; these are registry keys,
        # never requests to a guessed Greenhouse board.
        cur = conn.execute("""INSERT OR IGNORE INTO companies
            (name,ats_type,board_slug,career_source,active)
            SELECT ?,'greenhouse',?,?,? WHERE NOT EXISTS
            (SELECT 1 FROM companies WHERE career_source=?)""",
                           (name, 'career-site-' + key, key,
                            int(kind != 'blocked' and key not in UNVERIFIED_SOURCES), key))
        inserted += cur.rowcount
        if conn.execute('SELECT 1 FROM companies WHERE career_source=?', (key,)).fetchone() is None:
            raise ValueError('Career catalog key conflicts with an existing ATS record')
    # These feeds now pass live listing and full-description validation using
    # the existing additive observation policy. Enable their saved catalog rows.
    conn.execute("UPDATE companies SET active=1 WHERE career_source IN ('wipro','jpmorgan') AND active=0")
    conn.commit()
    print(f"Seeded {inserted} new companies.\n")


def fetch_all(conn):
    companies = conn.execute("SELECT * FROM companies WHERE active=1").fetchall()
    total_jobs = 0
    errors = []

    for company in companies:
        try:
            fetched = p.fetch_jobs_for_company(company)
            p.upsert_jobs(conn, company["id"], fetched)
            total_jobs += len(fetched)
            print(f"  OK   {company['name']:<20} ({company['ats_type']:<10}) -> {len(fetched)} jobs")
        except Exception as e:
            errors.append((company["name"], company["ats_type"], company["board_slug"], str(e)))
            print(f"  FAIL {company['name']:<20} ({company['ats_type']:<10}) -> {e}")

    print(f"\nTotal jobs fetched: {total_jobs}")
    print(f"Boards with errors: {len(errors)}")
    if errors:
        print("\nFailed boards (check these slugs manually):")
        for name, ats, slug, err in errors:
            print(f"  - {name} [{ats}/{slug}]: {err}")


if __name__ == "__main__":
    p.init_db()
    conn = sqlite3.connect(p.DB_PATH)
    conn.row_factory = sqlite3.Row

    seed_companies(conn)
    fetch_all(conn)

    # quick sanity check: show a handful of fetched jobs
    print("\n--- Sample of fetched jobs ---")
    rows = conn.execute(
        """SELECT j.title, c.name as company, j.location FROM jobs j
           JOIN companies c ON c.id = j.company_id
           WHERE j.is_active = 1 LIMIT 10"""
    ).fetchall()
    for r in rows:
        print(f"  {r['title']} @ {r['company']} ({r['location']})")

    conn.close()
