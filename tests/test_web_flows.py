import io
import json
import re
import sqlite3
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from backend.app import create_app
from backend.migrate import apply_migrations
from backend.store import location_suggestions
from backend.pipeline_skeleton import SCHEMA, _render_email_html, _render_email_text, filter_candidate_jobs, get_unsent_top_matches, store_score, upsert_jobs


class WebFlowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / "jobs.db"
        self.uploads = Path(self.directory.name) / "uploads"
        self.conn = sqlite3.connect(self.database)
        self.addCleanup(self.conn.close)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        apply_migrations(self.conn)
        self.app = create_app(self.database, self.uploads)
        self.app.testing = True
        self.client = self.app.test_client()
        for method in ("post", "get"):
            network = patch("backend.pipeline_skeleton.requests." + method, side_effect=AssertionError("Unexpected outbound request"))
            network.start()
            self.addCleanup(network.stop)
        self.conn.execute("INSERT INTO companies(id,name,ats_type,board_slug) VALUES(1,'Alpha','lever','alpha')")
        self.conn.execute("INSERT INTO companies(id,name,ats_type,board_slug) VALUES(2,'Beta','greenhouse','beta')")
        for number in (1, 2):
            raw = json.dumps({"content": "<h2>Requirements</h2><p>Python and SQL; complete JD tail.</p>", "employmentType": "FullTime"})
            self.conn.execute("""INSERT INTO jobs(id,company_id,external_id,title,url,location,raw_json,first_seen_at,last_seen_at)
                VALUES(?,?,?,?,?,'Remote',?,'2026-10-01T00:00:00+00:00','2026-10-01T00:00:00+00:00')""",
                (number, number, str(number), f"Software Engineer {number}", "https://example.test/apply", raw))
        self.conn.commit()

    def seed_user(self):
        self.conn.execute("INSERT INTO users(id,name,email,resume_text) VALUES(1,'Owner','owner@example.test','Python resume')")
        self.conn.execute("INSERT INTO accounts(user_id,password_hash,created_at) VALUES(1,'unused-test-fixture','2026-10-01T00:00:00+00:00')")
        for number, score in ((1, 85), (2, 75)):
            store_score(self.conn, number, 1, dict(fit_score=score, reasoning="Good fit", matched_skills=["Python"], missing_requirements=["SQL"]))
        self.conn.commit()
        with self.client.session_transaction() as session:
            session["user_id"],session["session_version"] = 1,0

    def post(self, path, data, client=None):
        client = client or self.client
        page = client.get("/settings")
        token = re.search(rb'name="csrf_token" value="([^"]+)"', page.data)
        self.assertIsNotNone(token)
        response = client.post(path, data=dict(data, csrf_token=token.group(1).decode()))
        self.addCleanup(response.close)
        self.addCleanup(response.request.input_stream.close)
        return response

    def settings_data(self, **overrides):
        values = dict(name="Owner", email="owner@example.test", university="College", graduation_year="2027",
                      cadence="daily", send_time="08:00", timezone="Asia/Kolkata", min_score="70", include_gaps="on",
                      roles="Software Engineer", locations="Remote", companies=["1"])
        values.update(overrides)
        return values

    def test_migration_is_idempotent_and_preserves_existing_records(self):
        self.seed_user()
        before = self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        apply_migrations(self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], before)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], 4)

    def test_location_suggestions_use_active_posting_terms_and_escape_markup(self):
        self.seed_user()
        self.conn.execute("UPDATE jobs SET location='Bengaluru, Karnataka, India' WHERE id=1")
        self.conn.execute("UPDATE jobs SET location='bengaluru, Karnataka' WHERE id=2")
        self.conn.commit()
        terms = location_suggestions(self.conn)
        cities = [item for item in terms if item["value"].casefold() == "bengaluru"]
        self.assertEqual(len(cities),1)
        self.assertEqual(cities[0]["count"],2)
        self.assertTrue(any(item["value"] == "Karnataka" for item in terms))
        page = self.client.get("/settings")
        self.addCleanup(page.close)
        self.assertIn(b'list="location-options"',page.data)
        self.assertIn(b'value="Bengaluru"',page.data)
        self.assertIn(b'value="Karnataka"',page.data)
        self.conn.execute("UPDATE jobs SET location='<script>unsafe</script>' WHERE id=1")
        self.conn.execute("UPDATE jobs SET is_active=0 WHERE id=2")
        self.conn.commit()
        self.assertFalse(any(item["value"].casefold() == "bengaluru" for item in location_suggestions(self.conn)))
        page = self.client.get("/settings")
        self.addCleanup(page.close)
        self.assertNotIn(b'value="<script>',page.data)

    def test_multiple_location_inputs_save_and_empty_means_anywhere(self):
        self.seed_user()
        response = self.post("/settings", self.settings_data(locations=["Bengaluru","Karnataka","", "Remote"]))
        self.assertEqual(response.status_code,302)
        prefs = json.loads(self.conn.execute("SELECT preferences_json FROM users WHERE id=1").fetchone()[0])
        self.assertEqual(prefs["locations"],["Bengaluru","Karnataka","Remote"])
        self.assertEqual(self.conn.execute("SELECT SUM(is_stale) FROM scores").fetchone()[0],2)
        self.assertEqual(self.post("/settings",self.settings_data(locations=[""]*20)).status_code,302)
        prefs = json.loads(self.conn.execute("SELECT preferences_json FROM users WHERE id=1").fetchone()[0])
        self.assertEqual(prefs["locations"],[])
        self.assertEqual(self.post("/settings",self.settings_data(locations=["Bengaluru"]*21)).status_code,400)

    def test_all_screens_have_working_navigation_without_profile(self):
        routes = ("/", "/jobs", "/settings", "/analytics", "/applications", "/resume")
        for route in routes:
            with self.subTest(route=route):
                response = self.client.get(route)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["Location"], "/login")
        self.seed_user()
        for route in routes:
            response=self.client.get(route)
            self.assertEqual(response.status_code,200)
            for target in routes:
                self.assertIn(('href="'+target+'"').encode(),response.data)

    def test_refined_screens_preserve_local_navigation_and_read_only_rendering(self):
        self.seed_user()
        before = self.database.read_bytes()
        for route in ("/", "/jobs", "/resume", "/settings", "/analytics", "/applications"):
            with self.subTest(route=route):
                response = self.client.get(route)
                self.assertEqual(response.status_code, 200)
                self.assertIn(b'href="/static/css/theme.css"', response.data)
                self.assertIn(b'href="/static/css/wow.css"', response.data)
                self.assertIn(b'class="hero-banner"', response.data)
                self.assertIn(b'href="#main">Skip to content', response.data)
                self.assertIn(b'aria-current="page"', response.data)
                self.assertIn(b'aria-hidden="true"', response.data)
                self.assertNotIn(b"fonts.googleapis.com", response.data)
                if route == "/settings":
                    self.assertEqual(response.data.count(b"<script"), 1)
                    self.assertIn(b'src="/static/js/locations.js"', response.data)
                else:
                    self.assertNotIn(b"<script", response.data)
                    self.assertNotIn("script-src", response.headers["Content-Security-Policy"])
                self.assertNotIn(b"Scheduled sending is not connected yet", response.data)
                self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])
        self.assertEqual(before, self.database.read_bytes())

    def test_banner_counts_use_saved_data_and_do_not_include_mockup_claims(self):
        self.seed_user()
        self.assertIn(b"2 current matches for your resume", self.client.get("/").data)
        self.assertIn(b"0 applications tracked", self.client.get("/applications").data)
        self.assertEqual(self.post("/applications", dict(job_id="1", status="interview")).status_code, 302)
        applications = self.client.get("/applications").data
        self.assertIn(b"1 application tracked", applications)
        self.assertIn(b"1 interview and 0 offers recorded", applications)
        for route in ("/", "/jobs", "/resume", "/settings", "/analytics", "/applications"):
            with self.subTest(route=route):
                page = self.client.get(route).data
                for claim in (b"Northwind Labs", b"82 out of 100", b"up to 10 points", b"61% to 78%", b"apply in one click"):
                    self.assertNotIn(claim, page)
                self.assertNotIn(b"cdnjs.cloudflare.com", page)
                self.assertNotIn(b"text/x-dc", page)

    def test_animated_screens_render_without_matches_or_application_history(self):
        self.seed_user()
        self.conn.execute("UPDATE scores SET is_stale=1")
        self.conn.commit()
        for route in ("/", "/jobs", "/resume", "/settings", "/analytics", "/applications"):
            with self.subTest(route=route):
                page = self.client.get(route)
                self.assertEqual(page.status_code, 200)
                self.assertIn(b'class="hero-banner"', page.data)
                self.assertNotIn(b'class="spark"', page.data)
        self.assertIn(b"0 current matches for your resume", self.client.get("/").data)

    def test_company_tiles_escape_untrusted_titles_and_company_names(self):
        self.seed_user()
        self.conn.execute("UPDATE companies SET name='<script>company</script>' WHERE id=1")
        self.conn.execute("UPDATE jobs SET title='<script>role</script>' WHERE id=1")
        self.conn.commit()
        self.assertEqual(self.post("/applications", dict(job_id="1", status="saved")).status_code, 302)
        for route in ("/", "/jobs", "/applications"):
            with self.subTest(route=route):
                page = self.client.get(route).data
                self.assertIn(b'class="logo-tile" aria-hidden="true">&lt;', page)
                self.assertIn(b"&lt;script&gt;company&lt;/script&gt;", page)
                self.assertIn(b"&lt;script&gt;role&lt;/script&gt;", page)
                self.assertNotIn(b"<script>company", page)
                self.assertNotIn(b"<script>role", page)

    def test_sparklines_use_saved_weekly_values_and_handle_flat_or_single_series(self):
        self.seed_user()
        self.conn.execute("UPDATE scores SET scored_at='2026-09-28T00:00:00+00:00',fit_score=50 WHERE job_id=1")
        self.conn.execute("UPDATE scores SET scored_at='2026-10-05T00:00:00+00:00',fit_score=50 WHERE job_id=2")
        self.conn.commit()
        with patch("backend.app.utc_now", return_value=datetime(2026,10,7,tzinfo=timezone.utc)):
            for route in ("/", "/analytics"):
                page = self.client.get(route)
                self.assertEqual(page.status_code, 200)
                sparks = re.findall(rb'<svg class="spark".*?</svg>', page.data, re.DOTALL)
                self.assertTrue(sparks)
                for spark in sparks:
                    element = ET.fromstring(spark.decode())
                    self.assertEqual(element.find("path").attrib["d"].strip(), "M0.0 28.0 L100.0 28.0")
            self.conn.execute("UPDATE scores SET scored_at='2026-10-05T00:00:00+00:00'")
            self.conn.commit()
            for route in ("/", "/analytics"):
                self.assertNotIn(b'class="spark"', self.client.get(route).data)

    def test_location_picker_has_one_input_and_preserves_escaped_selections(self):
        self.seed_user()
        locations = ["Bengaluru", "Karnataka", '<script>unsafe</script>']
        self.conn.execute("UPDATE users SET preferences_json=? WHERE id=1", (json.dumps(dict(locations=locations)),))
        self.conn.commit()
        page = self.client.get("/settings")
        fields = re.findall(rb'<input\b[^>]*name="locations"[^>]*>', page.data)
        self.assertEqual(len(fields), 4)
        self.assertEqual(sum(b'type="hidden"' not in field for field in fields), 1)
        self.assertIn(b'id="location-input"', fields[0])
        self.assertIn(b'aria-label="Remove Bengaluru"', page.data)
        self.assertIn(b'value="&lt;script&gt;unsafe&lt;/script&gt;"', page.data)
        self.assertNotIn(b'<script>unsafe</script>', page.data)
        self.assertNotIn(b"More locations", page.data)
        # The unscripted input and hidden selections use the existing form shape.
        self.assertEqual(self.post("/settings", self.settings_data(locations=["Bengaluru", "Karnataka", "Remote"])).status_code, 302)
        saved = json.loads(self.conn.execute("SELECT preferences_json FROM users WHERE id=1").fetchone()[0])
        self.assertEqual(saved["locations"], ["Bengaluru", "Karnataka", "Remote"])

    def test_location_script_nonce_matches_policy_and_changes_per_response(self):
        self.seed_user()
        nonces = []
        for _ in range(2):
            page = self.client.get("/settings")
            nonce = re.search(rb'<script src="/static/js/locations.js" nonce="([^"]+)" defer>', page.data)
            self.assertIsNotNone(nonce)
            value = nonce.group(1).decode()
            nonces.append(value)
            policy = page.headers["Content-Security-Policy"]
            self.assertIn("script-src 'nonce-" + value + "'", policy)
            self.assertIn("script-src-attr 'none'", policy)
            self.assertIn("form-action 'self'", policy)
            self.assertIn("frame-ancestors 'none'", policy)
            self.assertNotIn("script-src 'self'", policy)
            self.assertNotIn("script-src 'unsafe-inline'", policy)
        self.assertNotEqual(nonces[0], nonces[1])
        script = self.client.get("/static/js/locations.js")
        self.addCleanup(script.close)
        self.assertEqual(script.status_code, 200)
        self.assertNotIn("script-src", script.headers["Content-Security-Policy"])

    def test_cadence_radio_selection_round_trips_all_existing_values(self):
        self.seed_user()
        for cadence in ("daily", "weekly", "off"):
            with self.subTest(cadence=cadence):
                self.assertEqual(self.post("/settings", self.settings_data(cadence=cadence)).status_code, 302)
                page = self.client.get("/settings")
                choices = re.findall(rb'<input type="radio" name="cadence" value="([^"]+)" ([^>]+)>', page.data)
                self.assertEqual(len(choices), 3)
                self.assertEqual([value.decode() for value, attributes in choices if b"checked" in attributes], [cadence])
                self.assertIn(b'form="settings-form"', page.data)
                self.assertEqual(self.conn.execute("SELECT cadence FROM user_settings WHERE user_id=1").fetchone()[0], cadence)

    def test_charts_use_actual_scores_and_counts_at_boundaries(self):
        self.seed_user()
        self.conn.execute("UPDATE scores SET scored_at='2026-10-01T00:00:00+00:00',fit_score=0 WHERE job_id=1")
        self.conn.execute("UPDATE scores SET scored_at='2026-10-08T00:00:00+00:00',fit_score=100 WHERE job_id=2")
        self.conn.commit()
        with patch("backend.app.utc_now", return_value=datetime(2026, 10, 9, tzinfo=timezone.utc)):
            page = self.client.get("/")
            chart = re.search(rb'(<svg class="trend-chart".*?</svg>)', page.data, re.S)
            self.assertIsNotNone(chart)
            root = ET.fromstring(chart.group(1))
            points = root.find("polyline").attrib["points"].split()
            self.assertEqual([tuple(map(float, point.split(","))) for point in points], [(40.0, 200.0), (580.0, 20.0)])
            self.assertEqual([entry.find("title").text for entry in root.findall("circle")], ["2026-W39: 0.0%", "2026-W40: 100.0%"])
            analytics = self.client.get("/analytics?weeks=4")
            self.assertEqual(analytics.status_code, 200)
            self.assertEqual(analytics.data.count(b'class="week-column"'), 2)
            self.assertEqual(analytics.data.count(b'height:180.0px'), 2)
            self.assertIn(b"1 matches", analytics.data)
            self.assertIn(b"100.0% average", analytics.data)
            self.assertIn(b'aria-current="true" href="/analytics?weeks=4"', analytics.data)
            # A single week must render without division by zero.
            self.conn.execute("UPDATE scores SET scored_at='2026-10-01T00:00:00+00:00'")
            self.conn.commit()
            self.assertEqual(self.client.get("/").status_code, 200)
            self.assertEqual(self.client.get("/analytics").status_code, 200)

    def test_chart_empty_states_do_not_invent_metrics(self):
        self.seed_user()
        self.conn.execute("UPDATE scores SET is_stale=1")
        self.conn.commit()
        dashboard = self.client.get("/")
        analytics = self.client.get("/analytics")
        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(analytics.status_code, 200)
        self.assertNotIn(b'class="trend-chart"', dashboard.data)
        self.assertNotIn(b'class="weekly-chart"', analytics.data)
        self.assertIn(b"No recent scores to chart yet", dashboard.data)
        self.assertIn(b"No current scores in this period", analytics.data)
        self.assertIn(b"Open tracking is not connected", analytics.data)
        self.assertNotIn(b"nan", analytics.data.lower())

    def test_profile_onboarding_and_subscriptions_affect_every_match_view(self):
        self.seed_user()
        response = self.post("/settings", self.settings_data())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.conn.execute("SELECT name FROM users").fetchone()[0], "Owner")
        self.assertEqual(self.conn.execute("SELECT company_id FROM company_subscriptions").fetchone()[0], 1)
        for number in (1, 2):
            store_score(self.conn, number, 1, dict(fit_score=85, reasoning="Fit"))
        for path in ("/", "/jobs"):
            response = self.client.get(path)
            self.assertIn(b"Software Engineer 1", response.data)
            self.assertNotIn(b"Software Engineer 2", response.data)
        self.assertEqual(self.client.get("/jobs?job=2").status_code, 404)
        self.assertEqual([row["job_id"] for row in get_unsent_top_matches(self.conn, 1)], [1])

    def test_no_subscriptions_means_no_matches_and_no_scoring_candidates(self):
        self.seed_user()
        self.assertEqual(self.post("/settings", self.settings_data(companies=[])).status_code, 302)
        self.assertNotIn(b"Software Engineer 1", self.client.get("/").data)
        user = self.conn.execute("SELECT * FROM users WHERE id=1").fetchone()
        self.conn.execute("UPDATE scores SET is_stale=1")
        self.conn.commit()
        self.assertEqual(filter_candidate_jobs(self.conn, user), [])
        self.assertEqual(get_unsent_top_matches(self.conn, 1), [])

    def test_settings_validation_is_atomic_and_csrf_is_required(self):
        self.seed_user()
        self.assertEqual(self.client.post("/settings", data=self.settings_data()).status_code, 403)
        for overrides in (dict(email="bad"), dict(companies=["999"]), dict(send_time="25:00"), dict(timezone="bad"), dict(min_score="101")):
            with self.subTest(overrides=overrides):
                self.assertEqual(self.post("/settings", self.settings_data(**overrides)).status_code, 400)
        self.assertEqual(self.conn.execute("SELECT name FROM users WHERE id=1").fetchone()[0], "Owner")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM company_subscriptions").fetchone()[0], 0)

    def test_email_off_and_threshold_are_honored(self):
        self.seed_user()
        self.post("/settings", self.settings_data(min_score="90"))
        store_score(self.conn, 1, 1, dict(fit_score=85, reasoning="Fresh"))
        self.assertEqual(get_unsent_top_matches(self.conn, 1), [])
        self.post("/settings", self.settings_data(min_score="40"))
        self.assertEqual(len(get_unsent_top_matches(self.conn, 1)), 1)
        self.post("/settings", self.settings_data(cadence="off", min_score="0"))
        self.assertEqual(get_unsent_top_matches(self.conn, 1), [])

    def test_all_populated_screens_render_and_static_styles_are_available(self):
        self.seed_user()
        for route in ("/", "/jobs", "/settings", "/analytics", "/applications", "/resume", "/static/css/sc.css", "/static/css/theme.css", "/static/css/app.css", "/static/css/wow.css"):
            with self.subTest(route=route):
                response = self.client.get(route)
                self.addCleanup(response.close)
                self.assertEqual(response.status_code, 200)

    def test_gap_preference_and_html_escaping_in_email_rendering(self):
        self.seed_user()
        self.post("/settings", self.settings_data(include_gaps=""))
        store_score(self.conn, 1, 1, dict(fit_score=85, reasoning="<script>unsafe</script>", matched_skills=["Python"], missing_requirements=["SQL"]))
        user = self.conn.execute("SELECT * FROM users WHERE id=1").fetchone()
        matches = get_unsent_top_matches(self.conn, 1)
        html = _render_email_html(user, matches)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<strong>Gaps</strong>", html)
        self.assertNotIn("Gaps:", _render_email_text(user, matches))

    def test_employment_preference_filters_scoring_candidates(self):
        self.seed_user()
        self.post("/settings", self.settings_data(companies=["1", "2"], employment_types=["internship"]))
        user = self.conn.execute("SELECT * FROM users WHERE id=1").fetchone()
        self.assertEqual(filter_candidate_jobs(self.conn, user), [])

    def test_migration_failure_rolls_back_without_marking_applied(self):
        second_path = Path(self.directory.name) / "invalid.db"
        with closing(sqlite3.connect(second_path)) as conn:
            conn.execute("CREATE TABLE scores(id INTEGER)")
            # Missing base tables makes a later migration statement fail.
            with self.assertRaises(sqlite3.OperationalError):
                apply_migrations(conn)
            self.assertEqual([row[1] for row in conn.execute("PRAGMA table_info(scores)")], ["id"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], 0)

    def test_resume_update_retains_scores_marks_stale_and_requeues(self):
        self.seed_user()
        self.assertEqual(self.post("/resume", dict(resume_text="Python SQL projects", consent="on")).status_code, 302)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT SUM(is_stale) FROM scores").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM resume_versions").fetchone()[0], 1)
        self.assertNotIn(b"Software Engineer 1", self.client.get("/").data)
        user = self.conn.execute("SELECT * FROM users WHERE id=1").fetchone()
        self.assertEqual(len(filter_candidate_jobs(self.conn, user)), 2)
        store_score(self.conn, 1, 1, dict(fit_score=90, reasoning="Updated"))
        self.assertIn(b"90%", self.client.get("/").data)

    def test_resume_txt_upload_is_private_and_duplicate_is_idempotent(self):
        self.seed_user()
        response = self.post("/resume", dict(resume_file=(io.BytesIO(b"Python SQL updated resume"), "../../resume.txt"), consent="on"))
        self.assertEqual(response.status_code, 302)
        files = list(self.uploads.iterdir())
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].parent, self.uploads)
        self.assertEqual(self.client.get("/uploads/" + files[0].name).status_code, 404)
        self.post("/resume", dict(resume_text="Python SQL updated resume", consent="on"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM resume_versions").fetchone()[0], 1)

    def test_invalid_resume_does_not_change_saved_data(self):
        self.seed_user()
        invalid = [dict(resume_text="", consent="on"), dict(resume_text="Changed without consent"),
                   dict(resume_file=(io.BytesIO(b"\xff"), "resume.txt"), consent="on"),
                   dict(resume_file=(io.BytesIO(b"data"), "resume.exe"), consent="on")]
        for data in invalid:
            with self.subTest(keys=list(data)):
                self.assertEqual(self.post("/resume", data).status_code, 400)
        self.assertEqual(self.conn.execute("SELECT resume_text FROM users WHERE id=1").fetchone()[0], "Python resume")

    def test_application_save_update_and_duplicate_preserve_stage(self):
        self.seed_user()
        self.assertEqual(self.post("/applications", dict(job_id="1", status="saved")).status_code, 302)
        application_id = self.conn.execute("SELECT id FROM applications").fetchone()[0]
        self.post("/applications", dict(application_id=str(application_id), status="interview", notes="Interview scheduled"))
        self.post("/applications", dict(job_id="1", status="saved"))
        row = self.conn.execute("SELECT * FROM applications").fetchone()
        self.assertEqual(row["status"], "interview")
        self.assertEqual(row["notes"], "Interview scheduled")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0], 1)
        self.assertIn(b"Interview scheduled", self.client.get("/applications").data)
        self.assertIn(b"100.0%", self.client.get("/analytics").data)

    def test_manual_application_and_user_scoping(self):
        self.seed_user()
        self.assertEqual(self.post("/applications", dict(title="External role", company_name="Outside", status="applied")).status_code, 302)
        self.conn.execute("INSERT INTO users(id,name,email,resume_text,active) VALUES(2,'Other','other@example.test','Other',0)")
        self.conn.execute("""INSERT INTO applications(id,user_id,title,company_name,status,created_at,updated_at)
            VALUES(50,2,'Private role','Other company','saved','2026-10-01','2026-10-01')""")
        self.conn.commit()
        self.assertEqual(self.post("/applications", dict(application_id="50", status="offer")).status_code, 400)
        self.assertEqual(self.conn.execute("SELECT status FROM applications WHERE id=50").fetchone()[0], "saved")
        self.assertNotIn(b"Private role", self.client.get("/applications").data)

    def test_saved_search_and_job_details_use_complete_jd(self):
        self.seed_user()
        self.assertEqual(self.post("/jobs/searches", dict(name="Remote", q="Engineer", min_score="80", location="Remote")).status_code, 302)
        self.assertIn(b"Remote", self.client.get("/jobs").data)
        response = self.client.get("/jobs?job=1")
        self.assertIn(b"complete JD tail", response.data)
        self.assertIn(b"Python", response.data)
        self.assertNotIn(b"<h2>Requirements</h2>", response.data)
        response = self.client.get("/jobs?job_type=full-time&location=Remote&min_score=80")
        self.assertIn(b"Software Engineer 1", response.data)
        self.assertNotIn(b"Software Engineer 2", response.data)

    def test_changed_job_marks_scores_stale(self):
        self.seed_user()
        upsert_jobs(self.conn, 1, [dict(external_id="1", title="Software Engineer 1", raw_json={"content":"Updated requirements"})])
        self.assertEqual(self.conn.execute("SELECT is_stale FROM scores WHERE job_id=1").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT is_stale FROM scores WHERE job_id=2").fetchone()[0], 0)

    def test_invalid_scoring_outputs_are_not_persisted(self):
        self.seed_user()
        for result in ([], {"fit_score":101,"reasoning":"Invalid"}, {"fit_score":True,"reasoning":"Invalid"},
                       {"fit_score":80,"reasoning":""}, {"fit_score":80,"reasoning":"Fit","matched_skills":"Python"}):
            with self.subTest(result_type=type(result).__name__), self.assertRaises(ValueError):
                store_score(self.conn, 1, 1, result)
        self.assertEqual(self.conn.execute("SELECT fit_score FROM scores WHERE job_id=1").fetchone()[0], 85)

    def test_host_protection_and_payload_limits(self):
        self.assertEqual(self.client.get("/", headers={"Host":"attacker.example"}).status_code, 403)
        self.seed_user()
        response = self.post("/resume", dict(resume_file=(io.BytesIO(b"x" * (3 * 1024 * 1024)), "large.txt"), consent="on"))
        self.assertEqual(response.status_code, 413)


if __name__ == "__main__":
    unittest.main()
