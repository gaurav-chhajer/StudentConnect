# PostgreSQL migration plan

Target: PythonAnywhere PostgreSQL add-on. No production migration has been run.

## Immediate recovery

The release loader must report SQLite backup errors and retain the previous release.
The error-handling fix does not repair the hosted filesystem or database. Resolve the
disk I/O error and obtain an integrity-checked, downloadable backup before cutover.
Do not remove database journal files or bypass the pre-migration backup.

## Implementation proposal

1. Use Psycopg 3 for PostgreSQL, subject to approval and verification of its exact
   version, Python 3.12 compatibility, license, and PythonAnywhere installation path.
   Keep secrets in the existing private environment mechanism. Define connection
   configuration only after inspecting the add-on's supplied connection settings.
2. Inventory every SQLite connection, transaction, SQL query, and error handler in
   backend/ and scripts/. Introduce explicit PostgreSQL connection ownership and
   dictionary rows; preserve bounded timeouts and parameterized statements.
   Update placeholders, PRAGMAs, INSERT OR statements, generated-ID retrieval,
   locking, and SQLite-specific date expressions deliberately.
3. Create a PostgreSQL baseline schema reflecting all existing SQLite migrations,
   constraints, indexes, and foreign keys. Keep applied SQLite migrations unchanged.
   Retain SQLite support for existing local workflows until their replacement is
   explicitly approved; avoid silently changing local database behavior.
4. Adapt web requests, hosted worker endpoints, queues, migration tools, and backups
   to the selected database. Preserve account isolation, CSRF, queue durability,
   idempotent scoring writes, and uncertain-email pause behavior.
5. Add offline coverage for connection/configuration failures and SQL changes.
   Run integration tests against an isolated PostgreSQL test database before any
   production connection. Verify commit/rollback behavior, concurrent queue claims,
   account isolation, and restart behavior. Run the full existing suite.
6. Implement a bounded, repeatable transfer command that refuses a populated target,
   preserves IDs and timestamps, respects foreign-key order, and resets PostgreSQL
   identity sequences. Never copy credentials or personal records into logs.

## Cutover and verification

- Provision the add-on and a separate test database. Record host, port, database,
  user, and required TLS settings privately; never put passwords in this document.
- Rehearse schema creation and transfer using an approved backup and isolated target.
- Schedule a maintenance window. Stop all writers, including local queue workers,
  before taking the final consistent SQLite backup.
- Verify source integrity and backup restoration. Transfer to the empty target.
- Compare table counts, primary IDs, foreign-key relationships, scores, queue state,
  account state, and email sent/pause records. Check identity sequences.
- Switch application configuration, reload, and verify login, dashboard, queued
  scoring with mocks, and stored email history before resuming workers.
- Do not trigger live scoring or emails as smoke tests without explicit permission.

## Rollback boundary

Before PostgreSQL accepts new writes, restore the previous application configuration
and release using the untouched SQLite database. After new PostgreSQL writes begin,
stop writers and reconcile those writes before rollback; simply switching back would
lose data. Keep backups until the migration has been reviewed and accepted.

## Approval and implementation readiness

- Psycopg 3 and the database-access change spanning more than five files are approved.
- Add-on provisioning status, PostgreSQL version, and isolated test database availability
  are pending confirmation. Do not request or record passwords in chat.
- Approve the maintenance window and live transfer separately after rehearsal passes.

Repository audit identified SQLite dependencies in request connections, account and
OAuth transactions, hosted worker transactions, task claims, ID retrieval, dashboard
date/JSON queries, schema introspection, and deployment backup/migration commands.
Local embedding caches are separate SQLite stores and should remain local.
The current development machine has no PostgreSQL server available; Docker's engine
is unavailable. Integration verification requires an isolated PostgreSQL instance.
