# StudentConnect

StudentConnect collects company job openings, compares them with a saved resume
using Groq, and prepares match-score email digests. The local Flask application
connects six screens: Dashboard, Job matches, My resume, Applications,
Analytics, and Settings.

The live pages adapt the latest references in `frontend/designs/animated/` with a
shared navy sidebar, blue/coral theme, glass cards, illustrated page banners,
company initial tiles, and reduced-motion support. Dashboard and Analytics
sparklines use saved weekly scores and counts; unsupported mockup metrics remain
identified as unavailable. The live effects use server-rendered SVG and CSS;
the reference React/Three.js runtimes are not loaded by the application. Fonts
use local system fallbacks so the interface does not request external font services.

## Project layout

```text
backend/             Flask routes, SQLite queries, resume storage, matching pipeline
backend/migrations/  Versioned additive SQL migrations
frontend/templates/  Jinja pages and shared navigation
frontend/static/css/ Shared styles and account-page effects
frontend/static/js/  Location picker and account-page effects
frontend/designs/original/ Preserved earlier exports and unmodified runtimes
frontend/designs/updated/  Preserved newer six *-html design references
frontend/designs/animated/ Latest six *-html exports, including original CSS/JS/vendor files
scripts/             Explicit fetch, score, test-profile, and board-discovery commands
tests/               Offline unittest suites
data/boards/         Company-board CSV files
data/jobs.db         Local SQLite database (ignored by Git)
data/backups/        Database backups (ignored by Git)
data/uploads/        Private original resume files (ignored by Git)
wsgi.py              Hosted application entry point
requirements.txt     Pinned Python dependencies
.env.example         Configuration placeholders; never real credentials
```

The old root-level scripts have moved. Run the module commands below from the
repository root. Paths to the database and CSVs are anchored to the project,
not the shell's working directory. Do not serve the repository as a static site.
Design exports are reference files, not live templates. Edit `frontend/templates/`
and `frontend/static/` for application changes. All design collections retain
their original contents; root-level exports are also retained as supplied.
Existing backend, script, test and private-data paths
are unchanged; module commands still run from the repository root.

## Install and run locally

Use Python 3.12 and the project-local environment:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -B -m backend.migrate
.\.venv\Scripts\python.exe -B -m backend.app
```

Open `http://127.0.0.1:5000`. Sign-in is required for all six screens. Each account
sees only its own profile, resume, matches, searches, applications and analytics.
Without hosted configuration, the app rejects non-local Host headers.

Forms require CSRF tokens and the app limits upload sizes. Session signing keys
are generated in memory on startup, so a restart invalidates open forms; reload
the page before submitting. These local session settings are not a production
deployment configuration. Accounts expire after eight hours without cookie
refresh. Sign-out and password reset revoke all existing sessions for that account.

### Owner setup and invited accounts

Back up the database before applying `003_accounts.sql` through the migration
command above. This adds account/token tables and case-insensitive email uniqueness;
it does not change existing resumes, scores or profiles. A case-insensitive email
collision blocks migration and must be resolved deliberately. Do not edit applied
migrations or delete the old database. Existing profiles cannot sign in until linked.

Configure the existing Gmail environment variables, then explicitly request the
first owner invitation:

```powershell
.\.venv\Scripts\python.exe -B -m scripts.invite_owner
```

Enter the email already saved in your existing profile to retain its data and
confirm `SEND` to send a real invitation. Open `/register`, enter the emailed
43-character code, your email/name, and a new 12–128-character password. Ownership
is verified by access to that email. Registration links the existing user ID
without replacing profile data. The first owner can use **Invite testers** to
send invitations; testers cannot invite others. Public registration is disabled.

Invitations expire after 48 hours. Recovery is available from **Forgot password**;
enter its code on **Enter recovery code** within 30 minutes. Codes work once, are
stored only as SHA-256 hashes, and never appear in URLs or application logs.
Passwords use Werkzeug PBKDF2-SHA256 with 600,000 iterations and random salts.
Verified account emails cannot be changed in Settings. New users initially follow
no companies; they must choose companies and upload their own resume.

Five failed logins lock an account for 15 minutes. The local beta also limits all
login/registration attempts to 50 and recovery/reset requests to 20 per five-minute
window across accounts. Recovery mail has a five-minute per-account cooldown.
Recovery responses deliberately do not reveal whether an email has an account;
delivery errors are logged without private message contents. Uncertain invitation
delivery retains the pending code; inspect Gmail before attempting another send.
No invitation or recovery emails are automatically retried. An owner invitation
cannot be issued while another unexpired owner invitation or any account exists.

## Connected flows

1. After accepting an invitation and signing in, open **Settings** to edit your
   profile, choose company boards, and save
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
3. Fetch postings and run scoring explicitly, or schedule the unified pipeline below:

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
jobs sent. A manual email command connects saved matches to Gmail SMTP:

```powershell
.\.venv\Scripts\python.exe -B -m scripts.run_local_email
.\.venv\Scripts\python.exe -B -m scripts.run_local_email --send
```

The first command previews eligible counts without sending or logging personal
content. `--send` sends a separate HTML/plain-text digest to each verified active
account's saved email. Each run selects at most five active, current, unsent matches per user from
chosen companies above the saved threshold; Off disables sending. Existing jobs
can appear in the first digest. Groq evaluations include categories, component
scores, eligibility blockers, and unknowns. Gap display follows the saved setting.
No scores are generated by this command; run scoring separately first.

Set `GMAIL_APP_PASSWORD` and `DIGEST_FROM_EMAIL` in the system/process environment.
For this setup, the sender account is `improvingjobsearch@gmail.com`. Enable Google
2-Step Verification and generate a 16-character App Password for that account;
use it instead of the normal account password. Spaces between groups are accepted.
Store the password locally through Windows **Edit environment variables for your
account**, never in source code, chat, shell history, or Git. Open a new terminal
after setting persistent variables. `.env.example` documents placeholders only;
the application does not load that file. No credentials are required for preview.
Delivery uses `smtp.gmail.com:465`, certificate-verified TLS, and a 15-second
timeout per network operation, with the sender address also used for login.
Successful SMTP acceptance marks the included jobs in `sent_log`; this does not
prove inbox delivery. Failures return exit code 1 without marking jobs sent.
The command serializes each user's local send with a SQLite write lock. Earlier
successful users remain recorded if a later user's send fails; the batch stops
on that failure. Profiles not yet linked to verified accounts receive no digests.
SMTP has no provider
idempotency key: uncertain timeouts or a crash after acceptance can cause duplicate
mail on a later manual send. Review Gmail Sent and local records before retrying.
There are no automatic email retries. Resend credentials are no longer used.

Manual sending honors Off but does not wait for a scheduled time.
See [Google App Password instructions](https://support.google.com/mail/answer/185833).
Use this account for low-volume testing; Gmail sending limits still apply.

### Automatic email delivery on Windows

Starting the Flask app or deploying it to PythonAnywhere does not schedule
fetching, scoring, or digests. `scripts.run_local_fetch` seeds the company list
from the verified board CSVs and fetches active boards only when explicitly run;
`scripts.run_local_score` is also a separate manual command. The optional Windows
task below sends existing saved matches only and does not update jobs or scores.
No PythonAnywhere scheduled task is installed by this repository.

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
confirm its prompt. Store `GMAIL_APP_PASSWORD` and `DIGEST_FROM_EMAIL` in persistent
Windows User/Machine environment variables first, then sign out/in so scheduled
processes inherit changes. No credentials are stored in the task definition.
Inspect or disable **StudentConnect Email Digests** in Windows Task Scheduler.
The computer must be awake and you must be logged in; this is not an always-on
hosted service. This task sends saved scores only: fetching/scoring must still
be run separately.

Automatic sends create `data/email_schedule.paused` before attempting delivery.
Success removes it; errors or crashes leave it to prevent automatic retries of
uncertain sends. Review Gmail Sent and the local send log before clearing it. After
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
credentials, resume files, and populated databases out of Git. The hosted WSGI
entry point explicitly loads a private `.env`; local module commands do not.

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

`GMAIL_APP_PASSWORD` and `DIGEST_FROM_EMAIL` are needed for authenticated Gmail
delivery. Storing email preferences alone does not schedule delivery.

## Unified fetch, score, and email pipeline

`scripts.run_pipeline` is one complete cycle for a scheduler on the **same machine
and database as the website**. It does not start a background thread in Flask.
Initialize/migrate the database and configure Groq and Gmail before enabling it.

Preview existing due matches without fetching, scoring, writing data, or sending:

```powershell
.\.venv\Scripts\python.exe -B -m scripts.run_pipeline
```

Execute a real cycle (makes external requests and sends due emails):

```powershell
.\.venv\Scripts\python.exe -B -m scripts.run_pipeline --run
```

The cycle seeds known boards, fetches active boards, then scores only verified
active accounts with a nonempty saved resume. Company subscriptions and existing
role/location/type filters apply. Current scores are skipped. Accounts least
recently scored run first, with up to five evaluations per account and twenty
total per cycle; `--max-scores 1` through `--max-scores 100` changes the total
budget. Existing bounded Groq 429 retries apply per evaluation, so this budget
is not a strict HTTP-request or token limit. Pending work continues next cycle.
Inputs are rechecked under a write transaction before saving a score.

Emails use the existing daily/weekly cadence, timezone, minimum score, selected
companies and unsent-job rules, with up to five matches per digest. A successful
empty board closes its old jobs. Malformed response shapes are rejected rather
than treated as empty boards. Failed boards retain old data and are excluded
from scoring and email for that cycle; successful boards continue. All-board
failures skip scoring/email. Groq failures retain completed scores; valid current
scores may still be emailed. Partial failures return exit code 1 for monitoring.

`data/pipeline.lock` prevents simultaneous unified cycles and automatically releases
its OS lock on exit/crash. The file remains. Run only one pipeline scheduler and
avoid running standalone fetch/score commands concurrently. Existing
`data/email_schedule.paused` protection also applies: uncertain delivery leaves
the marker and prevents future cycles until the owner reviews Gmail Sent and
send records. Do not automatically remove that marker or retry uncertain sends.

For **paid PythonAnywhere scheduling**, configure an hourly task with this Bash
command (adjust the home path if needed):

```bash
cd /home/GauravChhajer/StudentConnect && .venv/bin/python -m dotenv -f .env run --no-override -- .venv/bin/python -B -m scripts.run_pipeline --run
```

This loads the hosted private configuration before importing the pipeline. An
hourly scheduler delivers on the first run after each user's chosen time, not
necessarily at that exact minute. Its next cycles also pick up new accounts,
resumes, preferences and jobs. Check task logs for errors and backups/storage.
The current free PythonAnywhere plan has no scheduled tasks; this code alone
cannot activate hosted automation. Windows scheduling against the local database
does not process accounts stored in the separate hosted database. The existing
Windows email-only installer remains unchanged and does not fetch or score.

## Verification

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests
.\.venv\Scripts\python.exe -m pip check
```

Tests use temporary databases and mock HTTP calls. No formatter, linter, or type
checker is currently configured. `pip check` checks dependency consistency,
not known vulnerabilities.

## PythonAnywhere invited beta

Target: `https://gauravchhajer.pythonanywhere.com/`. These steps prepare an invited
beta, not an always-running job pipeline. Do not publish credentials or private
data to GitHub. Windows environment variables do not transfer to the server.

1. Push reviewed source changes yourself, then open a PythonAnywhere Bash console:

   ```bash
   git clone https://github.com/gaurav-chhajer/StudentConnect.git
   cd StudentConnect
   python3.12 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   ```

   If already cloned, review local changes before updating; do not overwrite them.
   Confirm your absolute project path using `pwd`.

2. Decide whether to start fresh or retain local data. For retention, stop local
   writers, take a SQLite backup, and upload that backup as `data/jobs.db` plus
   `data/uploads/` through your private PythonAnywhere Files interface. Preserve
   upload filenames and existing accounts. Never overwrite a populated hosted
   database. For a fresh installation, the migration creates the database:

   ```bash
   .venv/bin/python -B -m backend.migrate
   ```

   Back up any existing database before running migrations.

3. Create a private root `.env` using `.env.example` as the template. Set
   `STUDENTCONNECT_BASE_URL=https://gauravchhajer.pythonanywhere.com`, your existing
   Groq settings, `DIGEST_FROM_EMAIL=improvingjobsearch@gmail.com`, and the Gmail
   app password. Set `STUDENTCONNECT_SECRET_KEY` once to a random 64-character
   hexadecimal value. Generate it directly into the private file without printing:

   ```bash
   .venv/bin/python -c 'from dotenv import set_key; import secrets; set_key(".env", "STUDENTCONNECT_SECRET_KEY", secrets.token_hex(32))'
   chmod 600 .env
   ```

   Run the generation command only during initial setup: replacing the key logs
   everyone out. Existing process variables take precedence over `.env` values.
   WSGI rejects missing hosted settings or email credentials before serving.

4. In the **Web** tab, select manual configuration and Python 3.12. Set the virtual
   environment to the absolute `.venv` path. Replace the provider WSGI file with
   the following, correcting the project path to match `pwd`:

   ```python
   import sys
   sys.path.insert(0, "/home/GauravChhajer/StudentConnect")
   from wsgi import application
   ```

   Enable **Force HTTPS before Reload or inviting testers**. HTTPS redirection is
   handled by PythonAnywhere; the app uses secure cookies and accepts only the
   configured hostname. Do not map the repository, `.env`, `data/`, or uploads as
   public static directories. Flask already serves `frontend/static/`.

5. If there is no owner account or pending owner invitation, explicitly send the
   first invitation from the console:

   ```bash
   .venv/bin/python -m dotenv -f .env run --no-override -- .venv/bin/python -B -m scripts.invite_owner
   ```

   This prompts for confirmation and sends a real email. Use the existing profile
   email to retain its data. Register using the emailed code, then invite testers
   from the owner's Invitations page. Each tester chooses their own companies and
   uploads their own resume. Recovery emails include the hosted reset page URL.

Verify HTTPS redirection, sign-in after a Web Reload, registration, password
recovery, and isolation between two test accounts. Actual hosted Gmail and Groq
connectivity still need testing. Free-tier outbound allowlists can block boards
(especially Ashby), and new free accounts lack scheduled tasks. Run fetch, score,
and email commands manually with the same dotenv prefix; preview email before
adding `--send`. Web requests do not automatically fetch or score jobs. Arrange
private backups and monitor storage limits and web-app expiry.

Official setup: [Flask WSGI](https://help.pythonanywhere.com/pages/Flask/) and
[Force HTTPS](https://help.pythonanywhere.com/pages/ForcingHTTPS/).

## Remaining work

- Account lifecycle controls and verified hosted connectivity.
- PostgreSQL migration for growth beyond the SQLite beta.
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
