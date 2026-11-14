# Frontend organization

## Live Flask application

Flask renders user-facing pages from `templates/` and serves assets from `static/`. Keep account forms and authenticated workflows here so they retain the existing sessions, CSRF protection, email verification, recovery, Google sign-in, and server-side validation.

- `templates/base.html` provides the shared navigation and layout.
- `templates/dashboard.html`, `jobs.html`, `resume.html`, `applications.html`, and `settings.html` implement Home, Jobs, Resume, Tracker, and Settings with live Flask data. Analytics is a Tracker tab; Settings has Profile, Companies, and Email tabs.
- `templates/admin.html` and `admin_shell.html` implement the eleven database-backed admin pages. `backend/admin.py` enforces server-side roles, validates actions, and records the audit log. Admin access never depends on an email prefix.
- `templates/account.html` handles sign-in, sign-up, verification, and recovery.
- `static/css/jobspresso-ui.css` is the visual system copied from the latest UI reference; `jobspresso-ui-live.css` maps it onto the Flask screens.
- `static/js/` contains the live behaviors and supporting scripts.

## Design reference

`jobspresso.zip` is the updated student/admin visual reference. `jobspresso_ui/` preserves the earlier standalone reference. Their browser-only demo jobs, local-storage account state, sample scoring, and placeholder actions are not used by Flask. The ZIP's stylesheet is copied into `static/css/jobspresso-ui.css`; `admin-live.css` adapts the new controls to the Flask pages.

## Admin and analytics integration

Prepare a verified database backup before applying migration `033_admin_analytics.sql` with the existing migration command. It adds audit records, verification codes, score-rating snapshots, application events, first strong-match records and durable manual job closures. It rebuilds `applications` with the same IDs and columns to allow Screen, Assessment and No reply, preserving foreign-key references. The migration runner now handles complete SQL statements, including triggers. Do not deploy the new app before migrating the database; the previous app can still read the retained application columns, but cannot display the new stages.

Response/interview rates use sent applications at least 14 days old or already replied to. Replies include Screen, Assessment, Interview, Offer and Rejected. Interview history survives subsequent rejection. Fewer than five eligible applications displays counts. Median first-reply days excludes legacy transitions whose dates are unknown. Status changes are captured for web and worker writes, including direct SQL updates.

Strong matches count each user's first score of 80+ once, grouped Monday–Sunday in IST, retaining closed jobs and subsequent lower scores. Existing strong scores are seeded from their saved score date and marked historical: overwritten earlier scores cannot be reconstructed. The companion application count refers to the week's strong-match cohort, regardless of application date.

`/admin` opens Overview. Email and board-check forms require an explicit real-send/check selection; automated tests mock external services. Failed tasks and stale scores queue bounded batches of up to 100 users for the existing worker. Running tasks are preserved. Company source identities cannot be repointed to another board; create a new board instead. Pending invitations are not blindly resent. The email pause marker is read-only. Missing historical lockout, backup-verification, fetch and provider metrics are labelled unavailable or described according to what is actually measured. Alias changes remain in the existing reviewed taxonomy workflow.

Offline verification:

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests
.\.venv\Scripts\python.exe -B -m unittest tests.admin_browser_checks
```

The optional browser check uses the already installed repository-local Chromium and intercepts all requests through Flask's test client. Screenshots are saved under `tests/artifacts/admin-integration/` using synthetic fixtures only.

Local evaluation results, verification captures, and older static previews are kept in the root `not needed/` archive and are ignored by git. The supported regression suite remains in `../tests/` because CI runs it.
