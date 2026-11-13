# Jobspresso

A student job-search site: every job gets a 0 to 100 score from the student's resume and preferences.
Plain HTML, CSS and JavaScript. No build step and no dependencies.

## Run it
Open `index.html` in a browser, or serve the folder:

    python3 -m http.server 8000      # then visit http://localhost:8000

The Bricolage Grotesque font loads from Google Fonts (falls back to system fonts offline).

## Structure
    index.html        markup for all views: Home, Jobs, Resume, Tracker, Account, error screen, loading screen
    css/styles.css    design tokens (light and dark palettes) and all styles
    js/app.js         data, scoring, rendering, routing, theme toggle, pour animation, loading and error screens
    assets/           logo.svg and favicon.svg (J + coffee cup)

## Views and routes (hash based)
    #home  #jobs  #resume  #tracker  #account  #signin  #signup
    #loading (replays the loading screen)  #error (500)  #offline  any unknown hash shows the 404 screen

## Where to plug in your real product
- Jobs data: `JOBS` and the sample-data generator right after `LOCS` in `js/app.js`.
  Replace with an API. `renderJobs()` filters, groups by company and paginates in one place; swap it for a
  server call that returns one page at a time (needed once you reach many thousands of openings).
- Apply links: add a `url` field to each job. Until then Apply opens a placeholder (example.com).
- Scoring: weights in `W` (skills 60, role 15, location 10, pay 10, job type 5). Settings the user leaves
  empty are skipped and the rest are rescaled. Fit tiers: 80+ strong, 60 to 79 good, under 60 stretch.
- Sign in and sign up: `window.Jobspresso.auth.signIn / signUp / forgot` in `js/app.js` are demo stubs that
  succeed after a short delay. Replace them with calls to your backend (throw an Error to show a message on the form).
  Only the name and email are kept in localStorage (`jp-user`), never the password. "Continue with Google" is a placeholder.
- Resume tailoring (not built yet): `window.Jobspresso.tailorResume(job, resume)` is a stub that returns `{ready:false}`.
- Resume input: paste text or upload .txt/.md. PDF and Word parsing are not built yet.
- Colours: the CSS variables at the top of `css/styles.css` (light under `:root`, dark under `[data-theme="dark"]`).

## Notes
- All user data (resume, tracker, preferences, theme) is stored in the browser's localStorage.
- Listings are generated sample data for demonstration, not real jobs.
- There is no auto apply, by design.
