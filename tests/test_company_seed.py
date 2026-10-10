"""Company catalog additions preserve existing records and are idempotent."""

import io
import sqlite3
import unittest
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import patch
from contextlib import closing, redirect_stdout

from backend.pipeline_skeleton import SCHEMA
from backend.migrate import apply_migrations
from backend.career_sources import SOURCES
from scripts.run_local_fetch import seed_companies
from scripts import seed_companies as importer


class CompanySeedTests(unittest.TestCase):
    def test_postman_source_change_keeps_id_subscriptions_and_is_idempotent(self):
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.execute('PRAGMA foreign_keys=ON')
            conn.executescript(SCHEMA)
            apply_migrations(conn)
            conn.execute("INSERT INTO companies(id,name,ats_type,board_slug) VALUES(6,'Postman','greenhouse','postman')")
            conn.execute("INSERT INTO users(id,name,email,resume_text) VALUES(1,'Example','example@test.invalid','Python')")
            conn.execute("INSERT INTO company_subscriptions(user_id,company_id,subscribed_at) VALUES(1,6,'2026-10-10T00:00:00+00:00')")
            with redirect_stdout(io.StringIO()):
                seed_companies(conn)
                seed_companies(conn)
            self.assertEqual(conn.execute("SELECT id,career_source FROM companies WHERE name='Postman'").fetchall(),
                             [(6, 'postman')])
            self.assertEqual(conn.execute('SELECT user_id,company_id FROM company_subscriptions').fetchall(), [(1, 6)])
            self.assertEqual(conn.execute("SELECT name FROM companies WHERE career_source IN ('cvent','zs') ORDER BY name").fetchall(),
                             [('Cvent',), ('ZS',)])
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_new_companies_are_seeded_once_without_replacing_existing_company(self):
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.executescript(SCHEMA)
            apply_migrations(conn)
            conn.execute("INSERT INTO companies(name,ats_type,board_slug) VALUES('Existing name','lever','meesho')")
            original = conn.execute("SELECT id,name FROM companies WHERE board_slug='meesho'").fetchone()
            with redirect_stdout(io.StringIO()):
                seed_companies(conn)
                first_count = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
                seed_companies(conn)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0], first_count)
            self.assertEqual(conn.execute("SELECT id,name FROM companies WHERE board_slug='meesho'").fetchone(), original)
            self.assertEqual(conn.execute("SELECT name,ats_type,board_slug,active FROM companies WHERE board_slug='arcesiumllc'").fetchall(),
                             [('Arcesium', 'greenhouse', 'arcesiumllc', 1)])
            self.assertEqual(conn.execute("SELECT name,ats_type,board_slug,active FROM companies WHERE board_slug='apolloio'").fetchall(),
                             [('Apollo.io', 'greenhouse', 'apolloio', 1)])
            self.assertEqual(set(row[0] for row in conn.execute('SELECT career_source FROM companies WHERE career_source IS NOT NULL')), set(SOURCES))
            self.assertEqual(conn.execute("SELECT name,active FROM companies WHERE career_source IN ('icici','futuresfirst') ORDER BY name").fetchall(),
                             [('Futures First', 0), ('ICICI Bank', 0)])
            self.assertEqual(conn.execute("SELECT name,active FROM companies WHERE career_source IN ('wipro','jpmorgan') ORDER BY name").fetchall(),
                             [('JPMorgan Chase', 0), ('Wipro', 0)])

    def test_offline_import_backs_up_before_migration_without_overwriting_backups(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent, prefix='tmp-company-') as directory:
            database = Path(directory) / 'jobs.db'
            with closing(sqlite3.connect(database)) as conn:
                conn.executescript(SCHEMA)
                conn.execute("INSERT INTO companies(name,ats_type,board_slug) VALUES('Original','lever','original')")
                conn.commit()
            fixed = datetime(2026, 10, 10, tzinfo=timezone.utc)
            with patch.object(importer.pipeline, 'DB_PATH', str(database)), \
                    patch.object(importer, 'datetime') as clock, redirect_stdout(io.StringIO()):
                clock.now.return_value = fixed
                self.assertEqual(importer.main(), 0)
                backup = next((Path(directory) / 'backups').glob('*.db'))
                previous_bytes = backup.read_bytes()
                self.assertEqual(importer.main(), 1)  # Same timestamp cannot overwrite a backup.
                self.assertEqual(backup.read_bytes(), previous_bytes)
            with closing(sqlite3.connect(backup)) as saved, closing(sqlite3.connect(database)) as current:
                self.assertEqual(saved.execute('SELECT name FROM companies').fetchall(), [('Original',)])
                self.assertNotIn('career_source', [row[1] for row in saved.execute('PRAGMA table_info(companies)')])
                self.assertEqual(current.execute("SELECT id,name FROM companies WHERE board_slug='original'").fetchall(), [(1, 'Original')])
                self.assertEqual(current.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0], 6)

    def test_source_extension_preserves_existing_source_and_constraints(self):
        import hashlib
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.executescript(SCHEMA)
            conn.execute('CREATE TABLE schema_migrations(name TEXT PRIMARY KEY, checksum TEXT NOT NULL)')
            for path in sorted((Path(__file__).parents[1] / 'backend' / 'migrations').glob('*.sql')):
                if path.name.startswith('006_'):
                    break
                sql = path.read_text(encoding='utf-8')
                conn.executescript(sql)
                conn.execute('INSERT INTO schema_migrations VALUES(?,?)',
                             (path.name, hashlib.sha256(sql.encode()).hexdigest()))
            conn.execute("INSERT INTO companies(id,name,ats_type,board_slug,career_source) VALUES(7,'Wipro','greenhouse','career-site-wipro','wipro')")
            conn.commit()
            apply_migrations(conn)
            apply_migrations(conn)
            self.assertEqual(conn.execute('SELECT id,career_source,career_source_legacy FROM companies').fetchall(),
                             [(7, 'wipro', 'wipro')])
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO companies(name,ats_type,board_slug,career_source) VALUES('Invalid','greenhouse','invalid','unknown')")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO companies(name,ats_type,board_slug,career_source) VALUES('Duplicate','greenhouse','duplicate','wipro')")

    def test_catalog_key_collision_is_explicit_and_preserves_the_existing_record(self):
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.executescript(SCHEMA)
            apply_migrations(conn)
            conn.execute("INSERT INTO companies(name,ats_type,board_slug) VALUES('Existing','greenhouse','career-site-icici')")
            with self.assertRaisesRegex(ValueError, 'conflicts'):
                seed_companies(conn)
            self.assertEqual(conn.execute("SELECT name,career_source FROM companies WHERE board_slug='career-site-icici'").fetchall(), [('Existing', None)])
