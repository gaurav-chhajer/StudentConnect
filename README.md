# StudentConnect

StudentConnect collects company job openings, compares them with a saved resume
using Groq, and prepares match-score email digests. The local Flask application
connects six screens: Dashboard, Job matches, My resume, Applications,
Analytics, and Settings.

The live pages adapt the updated root-level `*-html` design references with a
shared navy sidebar, blue/coral theme, navigation icons, and reduced-motion
support. Dashboard and Analytics charts use saved scores and counts; unsupported
mockup metrics remain identified as unavailable. Fonts use local system fallbacks
so the interface does not request external font services.

## Project layout

```text
backend/             Flask routes, SQLite queries, resume storage, matching pipeline
backend/migrations/  Versioned additive SQL migrations
frontend/templates/  Jinja pages and shared navigation
frontend/static/     Shared styles adapted from the design exports
frontend/designs/    Original six design exports and their unmodified runtimes
scripts/             Explicit fetch, score, test-profile, and board-discovery commands
tests/               Offline unittest suites
data/boards/         Company-board CSV files
data/jobs.db         Local SQLite database (ignored by Git)
data/backups/        Database backups (ignored by Git)
data/uploads/        Private original resume files (ignored by Git)
```

The old root-level scripts have moved. Run the module commands below from the
repository root. Paths to the database and CSVs are anchored to the project,
not the shell's working directory. Do not serve the repository as a static site.

## Install and run locally

Use Python 3.12 and the project-local environment:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -B -m backend.migrate
.\.venv\Scripts\python.exe -B -m backend.app
```

Open `http://127.0.0.1:5000`. The app is a local, unauthenticated workspace for
exactly one active profile. It refuses to select between multiple active users
and rejects non-local Host headers. It is **not ready for public multi-user
hosting**: add authentication and authorization before opening access.

Forms require CSRF tokens and the app limits upload sizes. Session signing keys
are generated in memory on startup, so a restart invalidates open forms; reload
the page before submitting. These local session settings are not a production
authentication solution.

## Connected flows

1. Open **Settings** to create your profile, choose company boards, and save
   role/location/type preferences. No companies selected means no new matching
   or email candidates. Existing profiles continue matching all companies until
   company choices are explicitly saved.
   A single location input suggests city, state, and other location terms from active
   company postings as you type (for example `ben` suggests Bengaluru with its
   posting context). Selected locations appear below the input with remove buttons;
   the input clears for your next city or state. Add typed locations with Enter or
   Add, up to 20 preferences. Remove all selections for any location. Free-text entries and
   old comma-separated submissions remain supported. Suggestions cover up to 500
   common terms and update after fetching jobs; browser dropdown behavior varies.
   Bangalore/Bengaluru aliases are still matched literally, so select both when
   targeting all postings for that city. No external location service is used.
   The picker uses a local script authorized by a per-response CSP nonce on
   Settings only. Inline handlers and scripts without that nonce remain blocked.
   With JavaScript disabled, saved selections remain and the input accepts
   additional comma-separated locations when you save.
2. Open **My resume** to edit text or upload a `.pdf`, `.docx`, or UTF-8 `.txt`
   file (at most 2 MB; extracted text at most 100,000 characters). PDF extraction
   uses pypdf; DOCX extraction uses python-docx. PDFs are limited to 20 pages;
   scanned/image-only pages and encrypted files are rejected. DOCX paragraphs,
   nested tables, headers, and footers are included; text boxes, images, and
   tracked changes may be omitted. Review the saved extracted text before
   scoring and correct complex-layout reading order or missing text in the editor.
   Expanded DOCX contents and PDF page streams are limited to 10 MB; DOCX
   archives are limited to 1,000 entries and nested tables to 10 levels.
   Old `.doc` files are not supported. Consent is required. Original
   uploads are stored privately with generated filenames. Versions are retained.
3. Fetch postings and run scoring explicitly:

```powershell
.\.venv\Scripts\python.exe -m scripts.run_local_fetch
.\.venv\Scripts\python.exe -m scripts.run_local_score
```

These commands make real external requests and update SQLite. Scoring sends the
resume and JD to Groq and requires `GROQ_API_KEY`. They are never invoked by
opening a dashboard page. Changing a resume, job preferences, or stored posting
marks affected scores stale; old rows remain and the scoring command refreshes
them. Dashboards and email selection exclude stale scores.

4. Use **Job matches** to filter, save named searches, inspect the complete JD,
   save jobs, or track them as applied. Apply links open the company's website;
   status tracking does not submit an application. Update an existing stage on
   **Applications**. You can also add applications for outside roles manually.
5. **Analytics** uses saved scores and manually tracked application stages.
   Response rate treats interview, offer, and rejection as recorded responses;
   this is not automated employer tracking. Weekly charts cover current scores,
   not a complete immutable history of scoring runs.

Email preferences, including frequency, local send time/timezone, score threshold,
and inclusion of gaps, are stored. Candidate selection honors company choices,
thresholds, and Off; rendering honors the gap preference. The dashboard is only a candidate preview and does not mark
jobs sent. A manual email command connects saved matches to Resend:

```powershell
.\.venv\Scripts\python.exe -B -m scripts.run_local_email
.\.venv\Scripts\python.exe -B -m scripts.run_local_email --send
```

The first command previews eligible counts without sending or logging personal
content. `--send` sends a real HTML/plain-text digest to the sole active profile's
saved email. Each run selects at most five active, current, unsent matches from
chosen companies above the saved threshold; Off disables sending. Existing jobs
can appear in the first digest. Groq evaluations include categories, component
scores, eligibility blockers, and unknowns. Gap display follows the saved setting.
No scores are generated by this command; run scoring separately first.

Set `RESEND_API_KEY` and `DIGEST_FROM_EMAIL` in the system/process environment;
the sender must be allowed by your Resend account/domain configuration. A missing
or malformed sender is rejected. No credentials are required for count preview.
Successful API acceptance marks the included jobs in `sent_log`; this does not
prove inbox delivery. Failures return exit code 1 without marking jobs sent.
The command serializes local sends with a SQLite write lock and uses a payload
hash as Resend's idempotency key. Resend retains keys for 24 hours; uncertain
timeouts or a crash after API acceptance need provider review before retrying,
especially if the payload changes. There are no automatic retries.

Manual sending honors Off but does not wait for a scheduled time.
See [Resend idempotency documentation](https://resend.com/docs/dashboard/emails/idempotency-keys).

### Automatic email delivery on Windows

Preview whether a digest is due without sending:

```powershell
.\.venv\Scripts\python.exe -B -m scripts.run_local_email --scheduled
```

Scheduled mode supports Settings' `Asia/Kolkata` and `UTC` zones. Daily delivery
is due after the saved local time if no email was accepted on that local date.
Weekly delivery starts at the next eligible time, then waits seven local dates
after the last accepted digest. Manual sends also reset this cadence. When no
matches qualify, no email or send record is created. Missed times can catch up
later that day; old daily slots are not replayed.

The installer prepares a least-privilege task for the current Windows user,
checking every 15 minutes while logged in. It does not overwrite an existing
task. Preview installation without registering it:

```powershell
.\scripts\install_email_schedule.ps1 -WhatIf
```

To activate real automatic emails, run the same command without `-WhatIf` and
confirm its prompt. Store `RESEND_API_KEY` and `DIGEST_FROM_EMAIL` in persistent
Windows User/Machine environment variables first, then sign out/in so scheduled
processes inherit changes. No credentials are stored in the task definition.
Inspect or disable **StudentConnect Email Digests** in Windows Task Scheduler.
The computer must be awake and you must be logged in; this is not an always-on
hosted service. This task sends saved scores only: fetching/scoring must still
be run separately.

Automatic sends create `data/email_schedule.paused` before attempting delivery.
Success removes it; errors or crashes leave it to prevent automatic retries of
uncertain sends. Review Resend and the local send log before clearing it. After
confirming no accepted-but-unrecorded email exists, remove the marker to resume:

```powershell
Remove-Item -LiteralPath .\data\email_schedule.paused
```

If an email was accepted but not recorded locally, reconcile its included jobs
first; do not clear the marker and resend blindly. The task result is nonzero
while paused. No bounce/inbox-delivery tracking is implemented.

## Storage and migrations

SQLite is retained for local use. Personal records are linked to `user_id`, while
jobs/companies are shared. Database access is centralized enough to plan a later
PostgreSQL migration; that migration is not implemented yet. SQLite-specific SQL
will need adaptation and testing.

A pre-change backup is at `data/backups/jobs-before-web-flows.db`. Migrations run
transactionally and record checksums; never edit an already applied migration.
Back up the database before future migrations. Rollback of this change means
restoring a backup after explicitly considering any newer user data; there is no
automatic destructive rollback command.

## Configuration and scoring

Read API credentials from process environment variables. `.env.example` lists
placeholders; the application does not automatically load `.env` files. Keep
credentials, resume files, and populated databases out of Git.

The default Groq model is `qwen/qwen3.8-27b` (the previous Llama model was unavailable).
Explicit `GROQ_MODEL` environment overrides remain respected. Changing `GROQ_MODEL` requires
setting `GROQ_CONTEXT_TOKENS` to the selected model's documented context size.
Scoring extracts cleaned, complete descriptions from Greenhouse, Lever, and
Ashby. Missing or oversized descriptions are rejected without truncation. The
budget check uses a conservative UTF-8 byte estimate, not an exact tokenizer.

Groq generates five component scores (skills 40, relevant experience 25, role
alignment 15, education 10, other constraints 10), the total, and match category.
Education and other constraints each receive 7 when the JD states no requirement.
Confirmed mandatory eligibility contradictions cap the final score at 40;
missing information is not a blocker. Categories are Strong (80+), Good (65+),
Partial (45+), Weak (25+), and Not a Match (below 25).
Python rejects invalid totals, categories, ranges, and unsupported evidence
quotes; it does not substitute its own assessment.
New Groq requests include the complete resume and cleaned JD as numbered JSON lines.
For each match or blocker, Groq returns integer `jd_line_id` and `resume_line_id`
references; Python retrieves both exact original lines before validation and saving.
Missing or invalid references and model-written evidence quotes are rejected.
Previously saved evaluations
remain readable; no database migration is needed. New results record their
`evidence_format` as `source-line-ids-v1`.
The scoring command retries HTTP 429 up to three times per job, waiting at least
the integer `Retry-After` seconds, with an exponential minimum of 1, 2, then 4
seconds. Missing/invalid headers, waits above 120 seconds, or exhausted retries
stop the batch. Network errors and other HTTP errors stop without retries.
Ctrl+C interrupts waiting safely. Rerun after the limit
clears; already saved current scores are skipped. Invalid evaluations are reported
and left unsaved while the batch continues. Interrupting exits cleanly.
Unreadable or insufficient documents produce no saved score. Responses have a 4,000-token output budget.
The Job matches screen shows the breakdown, evidence, blockers, and unknowns.
This is an AI match estimate, not an employer ATS score or hiring probability.
Model and rubric version are stored with new evaluations; legacy scores remain
available without a breakdown. The additive `002_score_evaluation.sql` migration
preserves existing data. Back up and run the migration before starting the app.
No live scoring or email delivery happens when opening the dashboard; the scoring
command sends your saved resume and full JD to Groq and can incur API charges.

`RESEND_API_KEY` and `DIGEST_FROM_EMAIL` are needed for delivery from a
verified sender. Storing email preferences alone does not schedule delivery.

## Verification

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests
.\.venv\Scripts\python.exe -m pip check
```

Tests use temporary databases and mock HTTP calls. No formatter, linter, or type
checker is currently configured. `pip check` checks dependency consistency,
not known vulnerabilities.

## Remaining work

- Login, email verification, and authorization for public multi-user access.
- PostgreSQL migration and a hosting/deployment plan.
- OCR for scanned resumes and broader support for complex document layouts.
- Hosted scheduling, recovery of uncertain sends, and delivery/bounce tracking.
- Email-open tracking and overall resume-quality scores shown in the mockups.
- Better role/location aliases, compensation normalization, and verified board ownership.
- Upload/database retention, deletion/export controls, and an automated backup policy.
- Dependency security audit and production quality-tool configuration.

Board discovery is a separate network command: `python -m scripts.ats_board_finder`.
It writes `data/boards/discovered_boards_batch2.csv` and does not overwrite the
verified seed CSV. Inspect discoveries before importing them. The test-profile
script is optional: `python -m scripts.add_test_user`; edit its placeholder data
first and be aware it updates any profile with the configured email.
