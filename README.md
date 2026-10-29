# StudentConnect

StudentConnect collects company job openings, compares them with a saved resume
using Groq, and prepares match-score email digests. The local Flask application
connects six screens: Dashboard, Job matches, My resume, Applications,
Analytics, and Settings.

The live pages adapt the latest references in `frontend/designs/animated/` with a
shared navy sidebar, blue/coral theme, glass cards, illustrated page banners,
company initial tiles, and reduced-motion support. Dashboard and Analytics
sparklines use saved weekly scores and counts; unsupported mockup metrics remain
identified as unavailable. All pages also use the canvas background adapted from
`frontend/designs/backgrounds/updated/StudentConnect – Match Constellation Background.html`.
The earlier export is preserved directly in `frontend/designs/backgrounds/`.
Its linked nodes, cursor scanner, glows and click ripples are decorative; labels
show randomly generated 55–97% matches from the original design, unrelated to
saved job-match scores. Labels avoid visible text, controls, and cards, trying
above and then below each node. Nodes dim behind these areas. Custom containers
can use `data-bg-avoid`; `data-bg-ignore` excludes an element and its text from
avoidance. Bounds refresh on scrolling, resizing, and periodically during motion.
The animation uses
at most 90 nodes and 12 ripples, pauses in hidden tabs, and becomes a static scene
with reduced motion enabled. It uses local JavaScript with per-response CSP
nonces, without enabling inline scripts or loading external services.
Other live effects use server-rendered SVG and CSS;
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
frontend/designs/root-exports/ Former root-level *-html references, preserved unchanged
frontend/designs/backgrounds/ Original standalone background references
scripts/             Explicit fetch, score, test-profile, and board-discovery commands
tests/               Offline unittest suites
tests/artifacts/     Preserved historical test reports (not current verification)
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
their original contents; former root-level exports are retained unchanged under
`frontend/designs/root-exports/`.
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

### Email and Google signup

Google signup lets anyone with a Google account join without an invitation code.
Existing password login and invitation links remain available. Google signup
never grants administrator access. Set up the owner account below before opening
public signup; owner bootstrap requires that no accounts exist yet.

1. In Google Cloud, create an OAuth client of type **Web application**. Configure
   the consent screen for external users and publish its audience for public use;
   a client in testing mode only admits its configured test users.
2. Register the exact redirect URI:
   `https://gauravchhajer.pythonanywhere.com/auth/google/callback` for this hosted
   site, or `http://127.0.0.1:5000/auth/google/callback` for local development.
   Use that exact local origin when testing, rather than switching to localhost.
3. Store `STUDENTCONNECT_GOOGLE_CLIENT_ID` and
   `STUDENTCONNECT_GOOGLE_CLIENT_SECRET` privately on the server. Set both or
   neither; partial configuration fails startup. Never paste the secret into chat
   or commit it. Hosted WSGI loads `.env`; local module commands use process
   variables, as with the existing configuration.
4. Install `requirements.txt`, back up the existing SQLite database, then run
   `.\.venv\Scripts\python.exe -B -m backend.migrate` (Windows) or
   `.venv/bin/python -B -m backend.migrate` (hosting). The additive
   `011_google_accounts.sql` adds identity and temporary callback-state tables;
   `016_google_link_challenges.sql` adds short-lived email-code challenges for
   connecting an existing profile. Reload after configuration and migration.

Choose **Continue with Google** on Sign in or Sign up. Google verifies the email;
new accounts start with empty private profiles and no company subscriptions.
If that verified email already has a profile, Google sign-in sends a separate
one-time code to the email before connecting the Google identity. The code is
browser-bound, expires after 10 minutes and is consumed once. You can also sign
in with a password, open `/login`, and connect Google with the same email. Both
methods then open the same profile, resume and applications. A linked Google
identity cannot be reassigned to another profile. Google-only users can use
password recovery to set a password if mail delivery is configured. Different
email addresses remain separate profiles.

The flow validates the signed ID token, issuer, audience, expiry and nonce through
Authlib, and requires a verified email. Callback state expires after five minutes,
is consumed once, uses PKCE, and is bound to a separate HttpOnly callback cookie.
That temporary cookie uses SameSite=Lax for Google's redirect; authenticated
sessions retain SameSite=Strict. Google tokens are not saved. Existing login
limits and CSRF checks still apply. With Google credentials absent, verified-email
signup and password access continue and the Google button is hidden.

Test signup, returning login, cancelled consent and existing-account linking on
the actual hosted site before announcing public access. Outbound Google discovery,
token and signing-key requests must be allowed by the host. No Google credentials
or live sign-in are needed for the offline suite.

Reference: [Google web-server OAuth setup](https://developers.google.com/identity/protocols/oauth2/web-server)
and [Authlib Flask OpenID Connect](https://docs.authlib.org/en/stable/oauth2/client/web/flask.html).

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
send invitations; testers cannot invite others. Anyone can also request a public
email sign-up code on `/register`. It expires after 30 minutes and creates a
non-admin account only after the email, name and password are submitted together.
Existing invitations retain their 48-hour lifetime and owner permissions. Public
Google signup is available when Google credentials are configured.

Invitations expire after 48 hours. Recovery is available from **Forgot password**;
enter its code on **Enter recovery code** within 30 minutes. Codes work once, are
stored only as SHA-256 hashes, and never appear in URLs or application logs.
Passwords use Werkzeug PBKDF2-SHA256 with 600,000 iterations and random salts.
Verified account emails cannot be changed in Settings. New users initially follow
no companies; they must choose companies and upload their own resume.

Five failed logins lock an account for 15 minutes. The local beta also limits all
login/registration attempts to 50 and recovery/reset/sign-up-code requests to 20 per five-minute
window across accounts. Recovery mail has a five-minute per-account cooldown.
Recovery responses deliberately do not reveal whether an email has an account;
delivery errors are logged without private message contents. Uncertain invitation
delivery retains the pending code; inspect Gmail before attempting another send.
No invitation or recovery emails are automatically retried. An owner invitation
cannot be issued while another unexpired owner invitation or an owner account
exists. Public non-admin signups do not block owner setup; use an email that does
not already have an account for the first owner invitation.

## Connected flows

1. After accepting an invitation and signing in, open **Settings** to edit your
   profile, choose company boards, and save
   role/location/type preferences. No companies selected means no new matching
   or email candidates. Existing profiles continue matching all companies until
   company choices are explicitly saved.
   Until a resume and active company choices are present, every main page shows
   a setup checklist linking to **Upload resume**, **Review email**, and
   **Select companies**. Browsing remains available. Registration already saves
   the verified email; Settings displays it read-only. Completed steps are marked,
   and setup prompts disappear automatically when the required data is saved.
   Page banners prioritize setup actions while setup is incomplete.
   The minimum annual stipend field accepts 0 through 1,00,00,000 INR (1 crore
   per year); leave it blank for no minimum. Saved values are retained without
   monthly-to-annual conversion. Compensation matching remains approximate:
   posting amounts are not automatically normalized by payment period.
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

### Matching triggered by user changes

The additive `004_matching_tasks.sql` migration adds one durable task per user
and initializes tasks for existing active verified accounts. Back up the database
before running `python -B -m backend.migrate`; applied migrations stay unchanged.
Company selection/removal and matching-preference changes enqueue work in the
same transaction as Settings. Resume changes enqueue work with the saved version.
Unchanged saves and email-only preference changes do not enqueue new matching.
New/changed posting snapshots discovered by the pipeline queue following users.

Repeated changes coalesce into the existing task. A revision protects newer edits
from completion of older work. Task statuses are shown only to the signed-in user:
pending, in progress, complete or failed. Accounts without resumes remain pending
until a resume is saved. Budget-limited work remains pending; interrupted running
tasks are reclaimed on the next worker run under the existing process lock.
Failed matching is retried on the next worker cycle with the existing limits.

Process queued tasks without checking unrelated boards or sending emails:

```powershell
.\.venv\Scripts\python.exe -B -m scripts.run_pipeline --run --matching-only
```

Omit `--run` to preview the number of ready tasks without external calls or writes.
This mode checks current openings at each queued user's selected companies and
then scores missing/stale eligible matches. It processes up to 100 ready user
tasks per cycle, using the same scoring budgets as the full pipeline. Existing
current scores are reused, including when following a company again. Empty
company selections finish without scoring. Failed board refreshes retain old jobs
and mark matching failed rather than claiming that all openings were checked.

The matching-only worker needs Groq credentials but not Gmail; it can run while
email delivery is paused, leaving the email pause marker untouched. The normal
`--run` cycle also consumes these tasks, fetches new postings and sends due digests.
User requests only save tasks; they do not launch an unowned thread or make Groq
calls in a Flask request. Refresh a page after a worker run to see updated status.
Use a supported scheduler for periodic matching-only cycles and full cycles;
the worker exits after each batch, so it need not run continuously. Without a
scheduler or an explicit worker invocation, pending tasks will not process.

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
New free PythonAnywhere accounts have no scheduled tasks. The free GitHub worker
below can automate the hosted site instead. Windows scheduling against the local database
does not process accounts stored in the separate hosted database. The existing
Windows email-only installer remains unchanged and does not fetch or score.

### Run frequent hosted checks from your computer

`scripts.watch_hosted_jobs` runs on your PC against the PythonAnywhere database.
It now defaults to **local similarity**, with no Groq calls or LLM fallback.
Use `--fetch-only` for the five-minute job-fetch worker; that mode does no
comparison or email delivery. The separate daily comparison workflow remains
enabled. Groq remains an explicit `--provider groq` option for comparison mode.
This change does not deploy or restart the hosted site.

Publish the reviewed source through your normal deployment process. The
migration-aware deploy script backs up the hosted SQLite database and applies
`012_local_similarity.sql` before reloading; existing Groq scores and evidence
are retained. If the hosted checkout still has the older deploy script, follow
the one-time bootstrap instructions in the deployment section below. The
authenticated `similarity/work` and `similarity` endpoints are required; an
older server returns 404 and the worker stops. Install only `requirements.txt`
on the host: the embedding model runs on your PC, not in PythonAnywhere's web
process.

Configure the existing `STUDENTCONNECT_BASE_URL` and `STUDENTCONNECT_WORKER_KEY`
(matching the private hosted value) in your private local `.env` or process
environment. Local mode needs no Groq key. Never paste keys into commands or
logs. Resume text is processed on your PC and is not sent to a model service.
Private, user-scoped resume embeddings and extracted skill mentions are cached
in ignored `data/local_similarity_cache.db`; raw resume text is not stored there.
JD embeddings are shared between users. Treat vectors as private data too.

Prepare the CPU dependencies and pinned, public Apache-2.0 model on the PC:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-local.txt
.\.venv\Scripts\python.exe -B -m scripts.prepare_similarity --download
```

The explicit download uses no resume data and stores the model beneath
`data/models/`. Normal scoring loads only local files and fails with setup
instructions if the model is missing; it never silently downloads or uses Groq.
The model revision and scoring version participate in cache keys. The local
embedding/result caches each retain at most 50,000 entries, evicting older
insertions. Repeated text reuses cached embeddings, and resume caches include
the user ID. Result keys include user ID, resume-text hash, JD-text hash, model
revision and scoring version. Preference changes are rechecked on the server.

Long documents are split into overlapping tokenizer-sized chunks rather than
silently truncated to MiniLM's default context. Normalized chunk embeddings are
averaged and normalized again. Recognized skill names are compared locally;
mentions are not proof of proficiency, required qualifications, or eligibility.
The relevance index uses 70% nonnegative cosine similarity and 30% skill mention
overlap; with no recognized JD skills it uses semantic similarity alone. This
initial weighting is not a calibrated hiring prediction.

Open **Local similarity** in the sidebar (`/jobs/local`) to review rankings.
Changed resumes, preferences or posting content hide obsolete results until
rescored. Inactive jobs and unsubscribed companies are excluded. The local
worker scans eligible jobs in bounded pages rather than stopping at five jobs
per user, with up to 1,000 evaluations, 10,000 pages, and the existing 50-minute
time budget per cycle. Unfinished results remain pending for the next invocation.
Initial model loading and encoding can take much longer than cached comparisons;
benchmark your PC before choosing a refresh interval.

Preview without external requests:

```powershell
.\.venv\Scripts\python.exe -B -m scripts.watch_hosted_jobs
```

Start the five-minute fetch-only worker using `.env`:

```powershell
.\.venv\Scripts\python.exe -B -m dotenv -f .env run --no-override -- .\.venv\Scripts\python.exe -B -m scripts.watch_hosted_jobs --run --fetch-only
```

The fetch-only worker does not score jobs or send emails. Leave the separate
daily comparison workflow enabled; do not run another five-minute fetch worker.
To run local similarity comparisons instead, omit `--fetch-only`; local rankings
do **not** trigger fit-score email alerts: the old minimum-fit
threshold has not been calibrated for this new metric. Local mode does not call
the email endpoint. Existing fit evaluations remain visible in Job matches.
To explicitly resume the previous Groq scoring and immediate-email behavior,
configure `GROQ_API_KEY` (optional existing key slots still apply) and append
`--provider groq` to the command above. Gmail credentials stay on PythonAnywhere.

Add `--once` to perform one real cycle. Keep the terminal open and your computer
awake and online; Ctrl+C stops the worker. Disable the GitHub daily automation
workflow while using this worker, and run only one scheduler. No Windows task
is installed and no existing workflow is changed by these commands.

In explicit Groq mode, each cycle fetches active companies and scores pending candidates with the
existing cloud limits, then sends up to five qualifying unsent matches per
verified active account. Immediate delivery overrides daily/weekly timing for
email-enabled accounts; Off, minimum scores, subscriptions, stale-score checks,
sent-job tracking and uncertain-delivery pause protection still apply. Existing
eligible unsent matches from today's refreshed companies may also be sent on
the first run. The website's displayed daily/weekly settings still describe
scheduled digests, not this opt-in worker's immediate mode.

For the manual `scripts.watch_hosted_jobs` commands above, the next check starts
five minutes **after the previous cycle completes**. The Windows sign-in worker
below instead fetches independently on a five-minute schedule.
Large boards, description downloads, scoring budgets and Groq limits can delay
alerts substantially; this does not guarantee detection within five minutes.
The worker stops on an incomplete cycle or error rather than retrying uncertain
writes or sends. Review local/hosted logs before restarting. If delivery is
paused, reconcile Gmail and hosted send records before clearing the hosted
pause marker. Existing daily cloud commands retain their scheduled behavior.

To start the complete local flow (fetching, offline similarity, and Gmail SMTP)
when the current Windows user signs in, install its startup task:

```powershell
.\scripts\install_worker_startup.ps1 -WhatIf
.\scripts\install_worker_startup.ps1 -Confirm:$false
Start-ScheduledTask -TaskName 'StudentConnect Five Minute Worker'
```

The installer refuses to overwrite an existing task. It uses the repository's
`pythonw.exe` and private `.env`, preserving existing environment values, and
runs without a terminal window or administrator privileges. The launcher loads
the private `.env` before importing credential-reading modules. Configure local
`DIGEST_FROM_EMAIL` and `GMAIL_APP_PASSWORD`, prepare the downloaded model as above,
and use the existing migrated `data/jobs.db`. It never calls Groq or hosted worker
APIs, even if their credentials remain configured. No model is downloaded at runtime.
The public website, hosted database, and GitHub workflows are separate and are
not synchronized or changed by this local flow. Existing local accounts, resumes,
subscriptions, job history, and sent-job records are retained.
The worker starts at sign-in, runs on battery too, and needs an awake, online
laptop. Sleep pauses execution; opening the lid resumes a healthy running worker.
Fetching starts every five minutes measured from the previous fetch's start,
without waiting for Groq or emails. A fetch taking more than five minutes finishes
before the next starts; fetches never overlap. Successful snapshots immediately
enter the existing local SQLite database, which is the durable queue. New or changed
eligible jobs remain pending across restarts; current scores are excluded.
One independent consumer uses the installed CPU model to score bounded batches,
waking on a saved snapshot or polling every 30 seconds. It sends up to five unsent
jobs per verified active account per batch at the explicitly selected minimum
**60/100 local similarity** (`MIN_EMAIL_SIMILARITY` in `scripts/local_worker.py`).
These emails label similarity as a relevance ranking, never a fit percentage.
This separate threshold does not reinterpret the Groq minimum-fit setting.
As with the previous immediate-alert worker, Daily/Weekly timing is overridden;
Off, company subscriptions, current result hashes, and gap-display preferences
are honored. The existing `sent_log` email channel prevents repeat job alerts.
Budget-limited backlog continues in the next batch. Writes use the existing
pipeline lock; downloads and inference run outside SQLite write transactions.
Changed inputs are rechecked before saving; stale results are discarded.
Temporary board-read failures preserve jobs and do not cancel the next scheduled
fetch. Model or database errors stop the affected loop for inspection. A failed
email attempt disables emails for that process while scoring/fetching continue.
An uncertain SMTP send or sent-history failure leaves `data/email_schedule.paused`;
inspect Gmail and sent history before clearing it and restarting. No automatic
retry of uncertain emails occurs. SMTP acceptance does not prove inbox delivery.
Inspect both loops in `data/hosted_worker.log` (2 MB plus three rotated backups):
the task can remain Running with a failed loop. No new migration or dependency
is required. Resume and job text stay on the laptop for inference; ATS requests
and Gmail delivery still require the internet. No-alert reminder emails remain
a separate hosted feature and are not sent by the local similarity flow.

The local website reads the same database; rankings appear under `/jobs/local`.
To run it with private email settings while selecting localhost, use a separate
PowerShell terminal:

```powershell
.\.venv\Scripts\python.exe -B -m dotenv -f .env run --no-override -- .\.venv\Scripts\python.exe -B -c "import os; os.environ['STUDENTCONNECT_BASE_URL'] = ''; from backend.app import app; app.run(host='127.0.0.1', port=5000, debug=False)"
```

Open `http://127.0.0.1:5000/jobs/local` and sign in with a local account. The existing
worker key in `.env` keeps the web app's legacy Groq background matcher disabled;
keep it configured when using this separate local worker. Website startup itself
is manual; the five-minute automation task starts automatically at Windows sign-in.

Use Windows Task Scheduler to inspect **StudentConnect Five Minute Worker**, or:

```powershell
Get-ScheduledTaskInfo -TaskName 'StudentConnect Five Minute Worker'
Stop-ScheduledTask -TaskName 'StudentConnect Five Minute Worker'
Disable-ScheduledTask -TaskName 'StudentConnect Five Minute Worker'
```

#### No-alert reminders

The immediate hosted-email worker also checks for accounts that have gone seven
full days without a job-match email. For accounts that have never received one,
the window starts at verified account creation (invitation redemption). It sends
one reminder to review the website, resume and matching preferences, followed by
at most one follow-up fourteen full days after the first reminder. Reminders then
pause until a new match email or a detected resume, role/location preference,
minimum-score or company-subscription change starts another window. Profile
changes start their new window when first observed by the email worker.

Verified active accounts with email enabled are eligible. Off also disables
reminders. Pending/failed matching work, unscored candidates and qualifying
unsent matches block reminders. The local worker reaches email only after a
successful matching cycle; an outage must not be described as a poor resume.
Reminders never change preferences, and do not count as job-match emails.
They are checked on completed worker cycles, not at a guaranteed wall-clock time.
The same exclusive lock and uncertain-delivery pause protection cover reminders.

This feature required the additive `010_match_reminders.sql` migration. The
deployment script now makes and verifies a private SQLite backup before applying
pending SQL migrations, and reloads only after migration verification succeeds.
Existing users may receive their first reminder on the first healthy cycle if
their seven-day window has already passed.

### Free daily cloud automation

`.github/workflows/daily-automation.yml` runs on GitHub-hosted Linux runners,
even when your PC is off. It targets fetching/scoring at **06:30 Asia/Kolkata**
(01:00 UTC), fetching only at **18:30 Asia/Kolkata** (13:00 UTC), and emails at
**08:00 Asia/Kolkata** (02:30 UTC). Evening openings are scored during the next
morning run; the evening worker never scores or sends mail. GitHub schedules
can be delayed or missed; these are target times, not delivery guarantees.
The three jobs have 55-minute, 25-minute and 10-minute limits, including dependency
setup: 31 scheduled triples can use up to **2,790 job minutes**, exceeding a
2,000-minute allowance by 790 minutes if every job reaches its limit. Actual usage
depends on run duration. Other workflows, reruns and other repositories using the
same account also consume the allowance.
Do not add frequent runs or automatic workflow retries. Configure a zero-spend
GitHub Actions budget that stops usage at the free allowance, and monitor billing
usage. Public scheduled workflows can be disabled after 60 days of inactivity.
See [GitHub usage allowances](https://docs.github.com/en/billing/reference/product-usage-included)
and [scheduled workflow limitations](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

The website keeps its SQLite database and uploaded resume files. The worker
uses a separate authenticated API to fetch board definitions, save complete
snapshots, read pending resume text/job data, and save validated evaluations.
Resume text passes through GitHub's temporary runner and Groq; it is not written
to artifacts, caches, or logs by this worker. Restrict repository access and do
not enable verbose HTTP debugging. Gmail credentials remain on PythonAnywhere.
Browser login and CSRF protection remain required for browser actions; the worker
API instead requires its separate bearer credential and rejects session cookies.
Blank worker credentials disable API access. Invalid credentials fail startup.

The worker processes up to five missing/stale evaluations per account and 100
evaluations total per morning cycle. After existing preference/subscription filters,
Python ranks candidates by distinct resume keyword overlap with title/department
(weight 3) and the stored description (weight 1), ignoring common connecting words.
Repeated keywords do not add points; ties use ascending job ID. Missing descriptions
are logged and ranked using metadata until their selected posting is resolved.
This ranking only prioritizes the five candidates: it does not assign a final fit
score or permanently exclude lower-ranked jobs. It uses no external calls.
Its own 50-minute matching, 20-minute evening
fetch and 7-minute email budgets leave room within the job limits. The evening
fetch needs neither Groq nor Gmail credentials on GitHub. Reaching a limit reports
failure and defers unfinished work to another scheduled run. Transfers are limited to 2 MB;
oversized snapshots/batches fail explicitly without replacing stored snapshots.
Up to 1,000 companies/accounts and 5,000 jobs per snapshot are supported.
Failed boards retain previous jobs and are skipped for scoring.
Invalid evaluations and scoring HTTP 400/413/422 rejections defer that candidate
and continue with the remaining candidates. Cloud matching uses Groq only.
Every run starts with `GROQ_API_KEY` (Key 1). Only an explicit HTTP 429 advances
to `GROQ_API_KEY_2` (Key 2), then `GROQ_API_KEY_3` (Key 3); blank optional slots
are skipped and duplicate configured keys fail before network calls. The same
candidate is evaluated with the next configured key, which remains selected
for later candidates. Each configured key is attempted at most once per candidate.
No automatic key switch or retry occurs for authentication, timeouts, network
errors or HTTP 5xx. Gemini is no longer read or called, even if its old secret remains.
The complete resume/JD, rubric and evidence validation remain unchanged.
Groq retains its 30-second timeout. Every key attempt requires more than 240 seconds
remaining for status reports, evaluation, hosted save and headroom. Network timeouts
are inactivity limits, not guaranteed total durations. A recovered 429 alone does
not fail the run. Exhausted keys, authentication, network,
other HTTP failures, and hosted description/save failures stop the batch;
uncertain writes are not retried. Completed scores are retained. Any failure
still returns exit code 1. Logs include safe user/job IDs, operation stage and
HTTP status, without response bodies or credentials; the completed scoring
summary includes saved and failed candidate counts.
Later matching runs skip jobs with a current saved score for that user, including
after an interrupted run. Scores marked stale after resume or posting changes
remain eligible for rescoring. Fetching reserves the final two minutes of its
budget, leaving an 18-minute evening fetching window.
The worker records three Groq key slot statuses per matching run, including which
keys hit HTTP 429 and whether work completed or was deferred. The dashboard does
not display a Groq limit or key-history container. History remains stored for
diagnostics and is not included in email.
History is retained without automatic deletion and grows by three small rows per run.
Statuses describe observed evaluations, not live quotas or predicted reset times.
Key labels identify slots, not accounts; replacing a secret does not relabel old runs.
Interrupted runs remain marked unfinished with their last acknowledged key states.
No key values, fragments, hashes, account identities or provider response bodies are
stored. The worker publishes revisioned snapshots through its authenticated
`groq-status` endpoint with 10-second connect/20-second read timeouts. Invalid,
out-of-order or terminal-state changes are rejected; identical revisions are
idempotent. Status-write failures stop matching without retrying uncertain writes.
The additive `008_groq_key_history.sql` migration creates only history storage;
if its table is absent, the admin panel explicitly reports that the database update
is needed and logs the missing migration; saved dashboard analytics still load.
Existing jobs, users and scores are retained. Before the next matching run, deploy
the hosted endpoint and migration using the backup-producing offline import below,
then reload the web app. Do not edit applied migrations. An older runner still
works with the migrated host; the new runner requires the new endpoint. To roll
back application code, leave the additive history table in place to retain history.
Scoring candidate batches exclude failed boards
before the per-account limit, so retained jobs cannot crowd out refreshed boards.
The runner supplies successful company IDs through the optional `company_id`
query parameters on the internal work endpoint; omitted parameters preserve its
existing selection behavior. Deploy the hosted endpoint before the updated runner.
Email selection requires jobs refreshed on the current India date, current scores, saved minimum
scores, subscriptions and unsent-job eligibility. Email time is fixed at 08:00
India time; weekly cadence and Off remain honored. Saved local send-time/timezone
values are retained for local commands. The Settings page explains the fixed
cloud schedule when configured. No qualifying matches means no email.

Writes share `pipeline.lock` with CLI cycles. Input fingerprints and current
account/subscription checks reject evaluations made stale by edits. When the
worker key is configured, web requests no longer start the local matching thread;
new work waits for the daily GitHub run. Configure only one automation scheduler.
Email requests handle one account each with the existing SMTP/send-log protection.
Gmail acceptance is not proof of inbox delivery. Timeout, interruption, or uncertain
SMTP acceptance leaves `data/email_schedule.paused`; inspect Gmail Sent, bounce
messages, hosted logs and `sent_log` before reconciling and clearing the marker.
Never blindly rerun uncertain sends. Matching still runs when emails are paused.

### Additional target companies

The catalog adds ICICI Bank, Wipro, IDFC FIRST Bank, Futures First, Publicis
Sapient, JioHotstar (through JioStar), DP World, Bank of America, JPMorgan Chase,
Morgan Stanley, Apollo.io, Barclays, Nomura, Arcesium, Cvent and ZS, without duplicate entries.
Apollo.io and Arcesium use their verified Greenhouse boards. The enterprise
connectors use allowlisted official public sources; they do not guess ATS slugs.
`005_career_sources.sql` adds a nullable source selector and unique index. The
existing required ATS fields are compatibility metadata when that selector is
present, and are ignored by fetching and displayed as "Official career site".
Company IDs, subscriptions, users, jobs and scores are retained.

Postman now uses its official Workday board. The import updates the existing
Postman company's source in place, preserving its ID and subscriptions.
`006_extended_career_sources.sql` extends the allowed sources without modifying
migration 005: its original column and unique index are retained as legacy
metadata, and the current column has the extended validation and unique index.
Cvent and ZS use their official public iCIMS listing and description feeds.
The catalog also includes Salesforce, Adobe and Slack through their official
Workday boards, Spotify through Lever, Figma/Asana/Dropbox through Greenhouse,
Airbnb through Greenhouse and Notion through Ashby. Slack uses its dedicated
Salesforce-hosted board. These feeds cover worldwide listings; saved location
preferences still apply. Airbnb and Notion apply the existing senior-title
exclusion before transfer, preserving full descriptions and complete-feed
snapshot semantics within the current hosted limits. Zoom's official career
pages returned HTTP 403 and its catalog entry remains disabled; no access
controls are bypassed. Live checks verified listings, transfer size and full
descriptions for the nine enabled companies on 2026-10-21.
Migration `009_saas_company_sources.sql` extends the source allowlist additively,
preserving existing company IDs, subscriptions, jobs and users. Use the
backup-producing offline company import below before reloading the app.

The catalog also includes PwC, Google, Amazon (India), Apple (India), Netflix,
Microsoft and Meta. Cvent's existing entry and subscriptions are reused.
`007_major_company_sources.sql` extends source validation additively, retaining
the previous source column/index and existing company IDs, jobs and subscriptions.
Run the backup-producing offline import below before reloading the updated app;
the code change alone does not update an existing database.

PwC uses its worldwide Workday experienced-careers board; Google uses its
worldwide public career result/detail pages. Amazon uses its official India
search feed and retains the full description, basic and preferred qualifications.
Apple uses its India listings and full detail data, including both qualification
sections. Netflix uses its worldwide public Eightfold listings and structured
job descriptions; the board can include Netflix-affiliated businesses.
Apple's retail/corporate category is not treated as an employment type.
Google and Apple do not currently expose a verified employment-type field here.
These sources use additive observations, existing limits and senior-title filters.
Live verification exercised pagination, transfer-size checks and one full
description for each adapter. PwC returned a malformed later page; Amazon's
advertised count changed during collection; Netflix repeated some paginated IDs.
Validated observations were retained with warnings under the existing ingestion
policy; these checks do not establish complete, stable worldwide snapshots.

Microsoft's current public `/api/pcsx/search` protocol and structured full
job descriptions are supported with offline tests. Single-page live checks passed,
but the subsequent full-feed check returned HTTP 429, so Microsoft remains
unavailable pending successful end-to-end verification. Meta's public page still
has no verified listing connector. No access checks or rate limits are bypassed.

Target companies offers case-insensitive name search. Disabled companies are
visible only to admins in Settings and dashboard subscriptions; regular users see
active companies only. Disabled companies cannot be subscribed to, including by
admins.
It initially shows 12 entries; **View more** reveals 12 more at a time, and
**View fewer** collapses the list. Searching and collapsing preserve checkbox
selections, including selections outside the visible results. Without JavaScript,
all companies permitted for the signed-in account remain visible. Picker checks: `node tests/test_company_picker.cjs`.
Lever snapshots omit duplicate HTML fields only when usable plain versions
exist; complete descriptions, requirement lists and employment metadata remain.
This brings the verified Paytm snapshot below the existing 2 MiB transfer limit.

ICICI's encoded API response does not yet have a verified decoder. Futures
First's listing feed is reachable, but its listings have empty descriptions and
the inspected application page did not expose a usable full description. They appear as "Automatic fetching
unavailable" and cannot be subscribed to or fetched until a verified connector
is available. Their presence in the catalog is not a claim of working automation.
Morgan Stanley's connector covers its public student/graduate feed; Nomura's
connector covers its official India listings. JioHotstar uses the shared JioStar
career board, which includes roles for other JioStar businesses. Other new feeds
currently cover worldwide listings; saved user location preferences still apply.

Enterprise listing snapshots apply the existing senior-title exclusion before
transfer. Full descriptions are loaded for eligible scoring candidates and
validated/stored through the authenticated worker before evaluating. Description
failures never become fabricated summaries. Saved descriptions are retained while
listing metadata is unchanged; a description-only edit that does not change that
metadata is not detected by the listing fetch. Employment-type preferences still
require structured source data; leave those filters empty for sources without it.

Full live checks passed for Postman, Cvent, ZS, JioStar, Publicis Sapient, DP World,
IDFC FIRST Bank, Bank of America, Morgan Stanley, Barclays and Nomura during
development. Wipro and JPMorgan passed new live listing, transfer-size and
full-description checks using the existing additive observation policy. Wipro
returned 1,671 eligible jobs; JPMorgan returned 205. Both runs retained incomplete
observations because listing counts changed, and Wipro repeated IDs. These are
validated observations, not complete worldwide snapshots. Both sources are now
enabled on import, including existing inactive catalog rows; other companies'
activation choices are preserved. A source failure retains prior jobs and is
not reported as a successful empty board. Run the backed-up offline company
import described below and reload the app to apply activation to a hosted catalog.

Paginated career feeds use additive observations rather than complete snapshots.
Validated pages collected before a timeout or pagination failure are retained with
a warning; a failure before any validated page still fails the fetch. Identical
IDs are collapsed, and conflicting IDs are withheld for that run. Missing jobs
and empty observations never close stored jobs. Only new, changed or reactivated
observations enqueue matching. The server derives this policy from company
configuration and rejects conflicting worker modes. Cloud observations are split
within the existing job-count and 2 MiB transfer limits; accepted batches remain
saved if a later transfer fails. Batching happens after collection and deduplication,
so a process crash during collection cannot save those pages.

Greenhouse, Lever, Ashby, Morgan Stanley and Publicis Sapient retain complete-feed
snapshot behavior, including closure on successful empty results. Wipro uses the
site's date sort. Paginated sources can retain closed jobs until closure is confirmed
independently; absence from a crawl is not proof of closure. Automatic detail-page
closure is deferred until removed-job responses are verified. Existing activation
choices are preserved except for the requested Wipro/JPMorgan activation on import.

The 6:30 AM / 6:30 PM fetch and 8:00 AM email schedules,
runner-minute caps, authentication and email safeguards are unchanged. Large
worldwide feeds may not finish within the evening budget and wait for the morning
cycle; two scheduled fetch runs do not guarantee two complete refreshes of every
board. The 1,860-minute scheduled maximum excludes reruns and other workflows.

After publishing the reviewed changes and pulling them into PythonAnywhere, run
the offline import **before reloading the web app**:

```bash
cd ~/StudentConnect
python -B -m scripts.seed_companies
```

Use the same Python environment as the hosted app. This command creates a new,
uniquely named SQLite backup, checks it, applies migrations, and imports the
catalog without calling any career site, Groq, or email service. Existing backups
are not overwritten. Check that the import succeeded, reload the PythonAnywhere
web app, then select available companies in Settings and save.

For an explicitly authorized **read-only live** source check on a machine with
network access, run `python -B -m scripts.check_career_sources hotstar dpworld`
(or supply other registry keys from `backend/career_sources.py`). This checks
complete snapshots, transfer size, and one description; it does not write SQLite
or call Groq/email. Offline tests never run this command or contact real services.

Activation (no paid PythonAnywhere task is needed; apply catalog migrations first):

1. Review and publish this code to the GitHub repository's default branch, then
   deploy the same source to the existing PythonAnywhere checkout. Do not replace
   its `.env`, database, uploads, or backups. The workflow is schedule-only.
2. In the hosted private `.env`, add a separate `STUDENTCONNECT_WORKER_KEY`:
   64 random hexadecimal characters. Generate it into that file without printing
   it, using the hosted Bash console from the project directory:

   ```bash
   .venv/bin/python -c 'from dotenv import set_key; import secrets; set_key(".env", "STUDENTCONNECT_WORKER_KEY", secrets.token_hex(32))'
   chmod 600 .env
   ```

   Generate once; rotation requires updating the GitHub secret too. Keep the
   existing Gmail settings, signing key and HTTPS site origin. Reload the Web app
   and keep Force HTTPS enabled. Ensure company boards are already seeded using
   the documented setup; the cloud worker reads existing active boards.
3. In GitHub **Settings → Secrets and variables → Actions**, add repository
   secrets `STUDENTCONNECT_WORKER_KEY` (same private value) and `GROQ_API_KEY`.
   Add optional secrets `GROQ_API_KEY_2` and `GROQ_API_KEY_3` for the two ordered
   fallback slots. Save actual keys only through GitHub's private secret interface,
   never in source, logs or chat. The worker no longer references the Gemini secret
   or its model variables. Optional keys belong only on GitHub for cloud matching;
   PythonAnywhere needs the worker credential but not these Groq secrets. Local
   scoring commands continue using only the primary Groq key.
   Copy the worker key through the private provider interfaces only; never paste
   it into chat, source, logs, screenshots or workflow YAML. Add repository variable
   `STUDENTCONNECT_BASE_URL=https://gauravchhajer.pythonanywhere.com` (adjust only
   if your deployed hostname differs). The matching workflow uses the documented
   default Groq model/context; update both together if changing models.
4. Enable Actions, set the spending stop, and enable workflow failure notifications.
   Check all three runs daily initially. Confirm fetched postings, scores, Gmail Sent
   and hosted send records after the next scheduled morning runs. Actual provider
   connectivity and inbox delivery must be verified after activation.

The 1,860-minute cap covers this schedule, not all account activity. Quotas,
provider outages, hosted storage/CPU limits, web-app expiry and GitHub scheduling
can interrupt service. Keep private backups and monitor the PythonAnywhere app.

## Verification

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests
.\.venv\Scripts\python.exe -m pip check
node tests/test_constellation.cjs
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

## Automatic PythonAnywhere deployment from main

The deployment workflow tests the exact commit, uploads a bounded release archive
through the [PythonAnywhere Files API](https://help.pythonanywhere.com/pages/API/),
and reloads the web app. It does not use a Bash console. On reload, the root WSGI
loader verifies the archive, extracts it into `.releases/<commit>`, links the
existing private `data/` and `.env`, takes the pipeline lock, backs up the SQLite
database before pending migrations, checks application startup in a separate
process, and then records the active revision. The previous release remains on
disk and is served if extraction, migration, or the isolated startup check fails.
An unexpected failure during the final in-process import requires a new reload
after the cause is fixed. Application data and credentials remain
in `/home/GauravChhajer/StudentConnect`.

Keep `PYTHONANYWHERE_API_TOKEN` in GitHub Actions secrets. The existing PythonAnywhere
web app must continue to import `from wsgi import application` from that project
directory, and its virtual environment must already contain `requirements.txt`.
The workflow compares the hosted requirements file with the tested commit and stops
if they differ; dependency installation still requires a separate reviewed step.
Push to `main` or run **Actions > Deploy PythonAnywhere > Run workflow**. No console
ID or console opening is needed. The first API deployment uploads the loader and
WSGI entry point before reloading, so the old checkout need not be pulled manually.

The workflow shares the scheduled worker concurrency group. It waits up to three
minutes for `data/deployment-status.json` to report the exact run and commit. A
failed or uncertain API request stops without retrying mutations. Check that file,
the website, and the PythonAnywhere error log before retrying a timeout. Releases
are retained for rollback and use the free account's disk allowance; remove old
releases only after a separate backup and review. Private `data/boards` in the
existing checkout remains the board source for the web app.

## Personal local application worker

### Admin workflow monitor

Administrators have a **Workflow monitor** navigation tab at
`/admin/workflow-monitor`. The route requires an authenticated admin account and
only reads that account's workflows, scoring, application, and email history.
Changing the workflow ID cannot reveal another account's workflow. It does not
start workers, approve jobs, edit credentials, or send applications from the web.

The monitor separates local discovery, local relevance scoring, email delivery,
and the personal application agent. Select a workstream to see saved jobs,
original/final AI fit scores, whether a prepared resume was revised or retained,
confirmed application states, Notion states, and email history. Local relevance
is labelled separately from AI fit. Older materials without a recorded revision
flag say that the revision status was not recorded rather than guessing.
Local job history is limited to the latest 1,000 openings from the account's
configured target companies (or the existing all-companies default); counts
describe that window. Job lists page 20 at a time, and activity pages scan at most
100 events. Severity filtering applies to that event window; older activity is
available separately. Workflow source coverage remains limited by the original
search adapters.

Migration `015_workflow_monitor.sql` adds independent worker events and last-report
tables without changing existing job or application rows. Back up and apply it
with the existing initializer before restarting the workers:

```powershell
.\.venv\Scripts\python.exe -B -m scripts.run_recruiter init --run
```

Restart the website and existing workers to load the updated code. Worker starts
remain explicit; the monitor does not alter Windows startup tasks. New structured
events include fetch results, local scores, sent-digest jobs, fit evaluations,
resume changes, approval waits, and confirmed agent submissions. Old raw text logs
are not imported. Events store allowlisted codes, resource IDs, numeric metrics,
and UTC timestamps; no keys, raw exception messages, resumes, or provider payloads
are exposed. Read-only page polling occurs every 10 seconds, can be paused, and
preserves open details. A failed refresh retains the last snapshot.

Status reports show the last observed stage, not proof a process is alive. Reports
older than 15 minutes are marked stale unless a terminal state was reported.
Scheduled next checks are estimates. Errors use fixed explanations and recovery
steps; unknown causes are explicitly unknown. Monitoring writes follow existing
SQLite transactions and local worker locks. A telemetry database failure is not
silently ignored. These audit tables grow with recorded activity; no automatic
deletion or retention policy is introduced.

Monitor checks:

```powershell
.\.venv\Scripts\python.exe -B -m unittest tests.test_workflow_monitor
.\.venv\Scripts\python.exe -B -m unittest tests.monitor_browser_checks
```

### Five-minute Lever agent

The first autonomous submission path is `scripts.run_recruiter_agent`. It uses
the saved workflow, profile, scoring/rewrite checks, application states, and
optional Notion tracker. There are no new dependencies; apply the monitoring
migration above before starting the updated agent. Configure
the process credentials described below and create a workflow with `analyze`
first. Preview is read-only and makes no external calls:

```powershell
.\.venv\Scripts\python.exe -B -m scripts.run_recruiter_agent --workflow-id 1
.\.venv\Scripts\python.exe -B -m scripts.run_recruiter_agent --workflow-id 1 --run
```

`--run` authorizes discovery/scoring, preparation, and actual submission of the
explicitly selected Lever jobs in that workflow. The agent searches an analyzed
workflow once, exports the spreadsheet, and waits for numbered approval. In a
second terminal, inspect the workbook and select only Lever rows, for example:

```powershell
.\.venv\Scripts\python.exe -B -m scripts.run_recruiter approve --workflow-id 1 --numbers 2,5
```

The next check prepares reviewed materials and submits ready selected jobs.
Checks start every 300 seconds, or after the previous check finishes if it took
longer. One process owns the agent lock; each cycle also takes the existing
application lock. Approval remains possible between cycles. `--once --run`
performs one cycle and exits, including when waiting for approval. Ctrl+C stops
the worker; it does not install startup tasks or change the separate local
fetch/similarity/email worker.

Original scores must exceed 60; the final validated document must score at least
80, with 90 as the target for originals scoring 80-89. Existing factuality,
eligibility, freshness, salary, red-flag, answer, and duplicate checks remain in
force. Non-Lever selections stop this agent before preparation. Unknown required
answers and other review states are never guessed. Applied runs are skipped on
restart; failed, submitting, unknown, or uncertain Notion writes require manual
reconciliation. Any raised error stops the process without automatic retries.
Below-threshold or otherwise blocked materials produce exit code 2 for review.
Confirmed submissions are saved locally before optional Notion synchronization.
Diagnostics go to the existing private rotating `data/recruiter.log`.

This milestone processes one approval batch and exits when complete. It does not
automatically create new approval batches, continuously rescan a frozen workbook,
or fix portal access/structured-data limitations. Start a new analysis/workflow
for another search. Recruiter outreach/follow-up drafting remains available through
the six-stage commands below. The browser agent uses the tested Lever adapter;
free-form AI browser planning and other ATS submission paths are outside this
first milestone.

### Complete six-stage workflow

Use `scripts.run_recruiter` for the complete flow. It runs locally, uses the
existing private profile and provider configuration below, and adds no package
dependencies beyond the already optional browser installation. Existing database
records and the master resume are preserved. Initialize with a verified backup
before the additive `014_recruiter_workflow.sql` migration:

```powershell
.\.venv\Scripts\python.exe -B -m scripts.run_recruiter init --run
.\.venv\Scripts\python.exe -B -m scripts.run_recruiter analyze --run
# Use the workflow ID printed by analyze.
.\.venv\Scripts\python.exe -B -m scripts.run_recruiter search --workflow-id 1 --run
```

`analyze` produces 20 evidence-referenced titles, grouped by Strong fit, Stretch,
and Adjacent pivot, with 8–10 keywords per title and missing-evidence flags.
`search` searches the first ten titles and stops after exporting a ranked XLSX
workbook to private `data/recruiter-workflows/`. It prints the top ten matches.
The workbook contains Shortlist, Resume analysis, Source coverage, and Unverified
and excluded sheets. Numbering is frozen after search; refreshes require a new
workflow so approvals cannot silently move to different jobs. Spreadsheet strings
are inline text, preventing formula injection; posting URLs are clickable links.

Discovery attempts LinkedIn, Indeed, and Wellfound through public browser pages
and Greenhouse, Lever, and Ashby through configured active company boards,
printing progress for each source, configured board, posting verification, and
AI eligibility/scoring stage. The same diagnostics are retained privately in
`data/recruiter.log` (2 MB plus three rotated backups). If a command exits before
printing the workbook path, inspect the final log lines rather than assuming the
browser's last page identifies the failure. An interrupted `searching` workflow
with no saved rows can restart using the same workflow ID; it repeats discovery
and may repeat AI calls completed before the interruption.
Keep the research browser window open until discovery finishes. A closed browser
stops the search immediately. Verification warnings include safe reason codes;
zero verified postings leave the workflow incomplete without creating an approval
workbook. An older empty `awaiting_approval` workflow can also retry the same
search command; its old empty workbook contains no jobs to approve.
Browser lifecycle diagnostics distinguish page crashes, page/context closure,
and browser disconnects. `expected_cleanup=False` identifies events before normal
cleanup; the browser version is logged without page content or browser stderr.

Each
source's coverage, failures, and limits are recorded in the workbook. This is
not a claim of exhaustive global coverage: search is one accessible result page
per title, at most 1000 discovered links, and configured boards for ATS feeds.
Login gates, CAPTCHA, inaccessible pages, missing JobPosting structured data, and
unverifiable dates are reported. No bypass, private login session, or invented
listing is used. Every shortlisted URL was visited successfully at search time;
this does not guarantee that a listing remains open later.

Only the original `JobPosting.datePosted`, within the preceding seven days,
establishes recency; an ATS last-updated or republished timestamp is not substituted.
Explicit India/India-eligible remote and fresher/up-to-one-year evidence is
required. Salary below 8 LPA is excluded when annual INR data establishes it.
Missing salary, another currency, an unclear period, or a range crossing 8 LPA
remains unconfirmed. Such jobs can be reviewed, but are Caution and cannot be
automatically submitted. AI source-quote checks support these decisions; they are
not independent certification of the employer's claims.

After inspecting the spreadsheet, explicitly choose its job numbers. The following
commands are separate stages; `search` never automatically calls them:

```powershell
.\.venv\Scripts\python.exe -B -m scripts.run_recruiter approve --workflow-id 1 --numbers 1,3,7
.\.venv\Scripts\python.exe -B -m scripts.run_recruiter review --workflow-id 1 --run
.\.venv\Scripts\python.exe -B -m scripts.run_recruiter materials --workflow-id 1 --run
.\.venv\Scripts\python.exe -B -m scripts.run_recruiter export --workflow-id 1
```

Review covers all discovered rows, not just selected jobs, and assigns Apply,
Caution, or Skip with source-backed concerns. Market-pay comparisons remain
explicitly unverified without independent current evidence; the 8 LPA personal
minimum is enforced separately. Materials are generated only for selected jobs
with original scores above 60. They include a source-grounded resume rewrite,
a cover note under 120 words, changes, and gaps. One separate factual audit checks
the rewrite and cover note; a failed audit stops preparation. Real claims can
still require human review. Only a score-improving validated rewrite replaces the
original for this job; the master resume stays unchanged. An original score of
80–89 targets 90, with 80 remaining the submission floor. Originals already at 90+
keep their resume. Export produces numbered private DOCX/JSON materials, with the
exact upload document, score, cover note, changes, gaps and eventual outreach draft.
New preparation versions archive earlier documents and evaluations; confirmed or
uncertain submissions cannot be replaced. Saved-only tracking entries can be
submitted after approval and retain their existing personal notes.

Submit selected ready jobs using their spreadsheet numbers:

```powershell
.\.venv\Scripts\python.exe -B -m scripts.run_recruiter submit --workflow-id 1 --numbers 1,3 --run
.\.venv\Scripts\python.exe -B -m scripts.run_recruiter outreach --workflow-id 1 --run
.\.venv\Scripts\python.exe -B -m scripts.run_recruiter export --workflow-id 1
```

Submission requires your selected workflow, an unchanged resume/document, a
current seven-day posting, Apply verdict, confirmed 8 LPA pay, resolved eligibility,
and a score of at least 80. Deselecting a job revokes its submission eligibility.
There is no daily count limit. Supported Greenhouse/Lever/Ashby forms are handled
sequentially; other sites produce a manual handoff with materials. Unknown form
questions and site verification pause. Cover-note text is available to explicitly
labeled cover-letter/note fields. File attachments or custom forms needing further
input pause rather than receiving guessed answers. Confirmed submissions appear
in Applications and synchronize to the configured existing Notion tracker with
the spreadsheet number. Timeouts after submission intent are unknown and never
automatically retried. The older `run_applications` commands also enforce the new
selection, reviewed-material, and submission gates; old prepared runs are not
grandfathered into automatic submission.

Outreach searches for a verifiable contact on the posting or in accessible LinkedIn
company-recruiter profiles. It distinguishes a company recruiter from proven
ownership of a vacancy. Missing or inaccessible contacts are explicitly reported.
Messages are under 90 words and end with a question. They mention applying only
when the local tracker has an applied/interview/offer/rejection history for that
job; otherwise they express interest. These are drafts, never sent by this worker.

For follow-ups, configure the persistent Notion integration described below and
use its existing due dates. Without that integration, explicitly schedule local
dates; the worker never invents a follow-up schedule:

```powershell
# Local tracking only; use an actual confirmed application ID, not a spreadsheet number.
.\.venv\Scripts\python.exe -B -m scripts.run_recruiter schedule --application-id 1 --date 2026-11-02
.\.venv\Scripts\python.exe -B -m scripts.run_recruiter followups --run
.\.venv\Scripts\python.exe -B -m scripts.run_recruiter sync-notion --run
```

`followups` writes exact message drafts for saved due dates in the current Monday
through Sunday week in India (UTC+05:30, matching Asia/Kolkata). With Notion enabled,
manage follow-up dates in the existing Notion database; local `schedule` does not
modify Notion. It never sends messages or changes contact history. Failed/uncertain
Notion writes retain local submission records and never cause resubmission.

Offline verification:

```powershell
.\.venv\Scripts\python.exe -B -m unittest tests.test_recruiter_workflow tests.test_personal_applications
.\.venv\Scripts\python.exe -B -m unittest tests.browser_checks
```

Live source compatibility, generated resume layout, and real provider/account
connectivity still need verification before unattended use. All automated checks
use synthetic inputs and intercepted browser pages.

### Configuration and supporting browser commands

The optional worker uses a visible local Chromium browser for direct Greenhouse,
Lever and Ashby postings. It is separate from the existing fetch/scoring workers and
does not change your master resume or its saved match scores. Other sites and
unrecognized forms pause for review. Live site compatibility has not yet been
verified; passing local fixtures does not establish support for every ATS form.

Rules: only original Groq scores strictly above 60 are candidates. A validated
upload document must score at least 80 before submission. Originals from 80 to 89
get a 90 target, with 80 still the submission minimum. Originals already at 90 or
higher retain the original resume. The full recruiter workflow generates one
rewrite proposal with source-line references, checks for invented numbers, and
runs a separate factual audit against the original. Unsupported claims stop
preparation. A rewrite is retained only if its validated score improves;
score improvements are not guaranteed. The original and revised documents are scored with the same configured
Groq model/rubric; the best validated document is kept. Local similarity rankings
are not accepted as application fit scores.

Editable recruiter instructions live in `backend/recruiter_instructions.md`.
The complete file is loaded into each scoped Gemini request: your recruiter
context and all six workflow stages, including the stop after the search
spreadsheet and explicit selection of jobs before later stages. This includes
India or India-eligible remote, fresher/up to one year, an 8 LPA annual minimum,
and resume-evidence constraints. Keep the `## Workflow` separator intact. A final
scope instruction limits each call to its own JSON response. Python owns search,
files, approvals, submission, and tracking. The full workflow below implements
those stages and enforces its own India/India-eligible remote, up-to-one-year,
seven-day posting and annual INR 8 LPA checks. Saved dashboard preferences are
not overwritten. An explicitly selected workflow posting can come from a company
outside existing subscriptions; that selection does not subscribe the account.

Install the optional dependency and its browser inside the repository:

```powershell
.\.venv\Scripts\python.exe -B -m pip install -r requirements-browser.txt
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path (Get-Location) 'data/browser-binaries'
.\.venv\Scripts\python.exe -B -m playwright install chromium
.\.venv\Scripts\python.exe -B -m pip check
.\.venv\Scripts\python.exe -B -m unittest tests.browser_checks
```

Use process environment variables for `GROQ_API_KEY`, `GEMINI_API_KEY`, and an
explicit supported `GEMINI_MODEL`. This local worker does not load `.env`
automatically. Keep keys private; never put them in the profile or frontend.
If a provider request fails, the CLI prints the HTTP code and a fixed safe
diagnostic for recognized key, billing, model, token-limit, or schema errors.
It never prints the provider's raw response, request, resume, or credential.
Full recruiter workflow requests also log the endpoint/model, redacted authentication, key length and
format checks, detected-proxy flag, generation configuration, and input sizes.
Responses log status, selected non-cookie headers and byte count. Only recognized
generic error text is printed; arbitrary response text and successful bodies are
withheld. Credit balances are not supplied by generateContent: verify billing in
Google AI Studio rather than interpreting these logs as a credit-balance check.
After a failed `analyze`, correct the reported issue and rerun `analyze --run`;
do not repeat `init` just to diagnose a provider error. No retry is automatic.
Full recruiter generation requests allow 10 seconds to connect and 180 seconds
of read inactivity (previously 90 seconds), to accommodate thinking-model latency.
Both limits appear in request diagnostics. A timeout does not confirm whether
Google processed or billed the request; check the project before repeating calls.
The full workflow sends the complete saved resume to Gemini for analysis,
rewriting and factual auditing, alongside the relevant job/contact evidence.
The older section-ordering-only call withholds its first identity block. Groq
receives the complete resume for scoring. Review provider data handling before use;
Google's unpaid-service terms prohibit submitting personal or confidential
information. Select an appropriate billing-enabled project for personal resume
processing. See [Gemini terms](https://ai.google.dev/gemini-api/terms).

Copy `scripts/application_profile.example.json` to the ignored private path
`data/application-profile.json`, only if that destination does not already exist.
Replace its placeholders with your explicit local user ID, matching verified
email, and approved answers. It intentionally starts with invalid user ID 0.
Use exact visible form labels as answer keys, with string values. Consent
checkbox answers must be explicit `yes` or `no`; the worker never guesses consent,
work authorization, salary, sensitive demographic answers, or other questions.
Missing required answers, login, CAPTCHA, OTP, radio controls, and changed forms
pause before submission. Recognizable headings such as Education, Skills,
Experience, and Projects are needed for automatic section ordering. Review the
source resume's extracted text and layout first.

Notion is optional. Create a private Notion integration, share your existing
tracker with it, and configure both `STUDENTCONNECT_NOTION_TOKEN` and
`STUDENTCONNECT_NOTION_SOURCE`. For the existing Gaurav Job Applications tracker,
the data-source ID is `8e2b84c2-a7e8-4e6c-9194-44ee3e849f28` (distinct from the
database page ID). The chat's connector credentials cannot be reused by the
unattended local worker. The adapter preserves the existing schema: Company,
Role, Job link, Match score, Contact status, Application date, and Listing group.
It checks job links before applying and blocks records with existing history or
a Skip verdict. It updates/creates an Applied record only after browser
confirmation. No follow-up messages are sent and other tracker fields stay intact.

Without a Notion integration, use the existing StudentConnect Applications page.
First reconcile your previous Notion applications there, including uncertain
submissions, so they cannot be submitted twice. Only then set `history_checked`
to `true` in the private profile. A `false` value prevents live work without
Notion. Notion failures never silently disable duplicate checks.

After reviewing the additive migration, initialize the worker. This command
creates and verifies a uniquely named private SQLite backup before migrating:

```powershell
.\.venv\Scripts\python.exe -B -m scripts.run_applications init --run
.\.venv\Scripts\python.exe -B -m scripts.run_applications preview
```

Preview is offline and does not create application records. Test preparation
without opening application forms, then inspect results:

```powershell
.\.venv\Scripts\python.exe -B -m scripts.run_applications prepare --job-id 123 --run
.\.venv\Scripts\python.exe -B -m scripts.run_applications status
.\.venv\Scripts\python.exe -B -m scripts.run_applications export --run-id 1
```

Export writes the exact upload document into private `data/application-documents/`
without overwriting an existing changed file. Inspect its layout before enabling
unattended submissions; automated text checks do not verify visual layout.

`--run` authorizes the command's real Gemini/Groq, browser, and configured Notion
operations. Replace example IDs with your own. To submit one prepared resume or
run continuously for your chosen account:

```powershell
.\.venv\Scripts\python.exe -B -m scripts.run_applications submit --run-id 1 --run
.\.venv\Scripts\python.exe -B -m scripts.run_applications run --run --watch
```

There is no daily application cap. Jobs are processed sequentially in bounded
database pages. Watch mode checks every 60 seconds and stops with Ctrl+C. It
does not fetch jobs; keep your existing matching process running against the
same local database. Hosted-only matches are not automatically downloaded into
the local database. Provider failures stop work without automatic retries.

Every job gets a durable run record. Confirmed submissions appear in the existing
Applications screen; its badge still reflects the master-resume match, while
notes record the uploaded resume's score and run ID. The CLI status command
shows paused/failed work, targets, final scores, and Notion synchronization state.
A pre-submission form pause with a validated 80+ document can be resumed after
fixing its cause with `resume --run-id 1 --run`, then `submit --run-id 1 --run`.
Preparation failures without a validated document require diagnosis; this version
does not automatically regenerate or erase an existing run.

`submitting` after interruption or `unknown` after a timeout means the employer
may have received the application. Check the employer's confirmation before any
manual action; these states cannot be resumed or automatically retried. Notion
write uncertainty is recorded separately as `unknown`; inspect the tracker before
reconciling it manually. `sync-notion --run` handles pending confirmed records,
but never retries uncertain writes. Local records remain authoritative for
preventing another submission. Browser traffic is restricted to the supported
ATS hosts and required listed resources; no site protection is bypassed.

The default offline suite includes the workflow/provider tests. The optional
browser suite above fulfills every browser request locally and submits only
synthetic forms. No real applications, paid-provider calls, or Notion writes are
needed for either suite.

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
