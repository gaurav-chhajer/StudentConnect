# Frontend organization

## Live Flask application

Flask renders user-facing pages from `templates/` and serves assets from `static/`. Keep account forms and authenticated workflows here so they retain the existing sessions, CSRF protection, email verification, recovery, Google sign-in, and server-side validation.

- `templates/base.html` provides the shared navigation and layout.
- `templates/dashboard.html`, `jobs.html`, `resume.html`, `applications.html`, and `settings.html` implement Home, Jobs, Resume, Tracker, and Account with live Flask data.
- `templates/account.html` handles sign-in, sign-up, verification, and recovery.
- `static/css/jobspresso-ui.css` is the visual system copied from the latest UI reference; `jobspresso-ui-live.css` maps it onto the Flask screens.
- `static/js/` contains the live behaviors and supporting scripts.

## Design reference

`jobspresso_ui/` is the latest standalone visual reference. Its browser-only demo jobs, local-storage account state, sample scoring, and placeholder apply actions are not used by Flask. Port visual changes into the Jinja templates and Flask-served assets while preserving existing route and form behavior.

Local evaluation results, verification captures, and older static previews are kept in the root `not needed/` archive and are ignored by git. The supported regression suite remains in `../tests/` because CI runs it.
