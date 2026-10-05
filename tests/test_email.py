import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import requests
from backend import pipeline_skeleton as pipeline
from scripts.run_local_email import send_digest, schedule_due, main
from tests import test_web_flows
from tests.test_evaluation import sample_result


class EmailTests(unittest.TestCase):
    setUp = test_web_flows.WebFlowTests.setUp
    seed_user = test_web_flows.WebFlowTests.seed_user

    def test_daily_timezone_and_time_boundary(self):
        self.seed_user()
        self.conn.execute("INSERT INTO user_settings(user_id,cadence,send_time,timezone) VALUES(1,'daily','08:00','Asia/Kolkata')")
        self.conn.commit()
        before = datetime(2026,10,4,2,29,tzinfo=timezone.utc)
        due = datetime(2026,10,4,2,30,tzinfo=timezone.utc)
        self.assertFalse(schedule_due(self.conn,1,before))
        self.assertTrue(schedule_due(self.conn,1,due))
        self.conn.execute("INSERT INTO sent_log(user_id,job_id,channel,sent_at) VALUES(1,1,'email',?)", (due.isoformat(),))
        self.conn.commit()
        self.assertFalse(schedule_due(self.conn,1,datetime(2026,10,4,18,tzinfo=timezone.utc)))
        self.assertTrue(schedule_due(self.conn,1,datetime(2026,10,5,2,30,tzinfo=timezone.utc)))

    def test_weekly_due_and_scheduled_preview(self):
        self.seed_user()
        self.conn.execute("INSERT INTO user_settings(user_id,cadence,send_time,timezone) VALUES(1,'weekly','08:00','UTC')")
        self.conn.execute("INSERT INTO sent_log(user_id,job_id,channel,sent_at) VALUES(1,1,'email','2026-10-01T08:00:00+00:00')")
        self.conn.commit()
        self.assertFalse(schedule_due(self.conn,1,datetime(2026,10,7,8,tzinfo=timezone.utc)))
        self.assertFalse(schedule_due(self.conn,1,datetime(2026,10,8,7,59,tzinfo=timezone.utc)))
        self.assertTrue(schedule_due(self.conn,1,datetime(2026,10,8,8,tzinfo=timezone.utc)))
        self.assertEqual(send_digest(self.conn,scheduled=True,now=datetime(2026,10,8,8,tzinfo=timezone.utc)),1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sent_log").fetchone()[0],1)

    def test_invalid_schedule_fails_closed(self):
        self.seed_user()
        with self.assertRaises(ValueError):
            schedule_due(self.conn,1,datetime(2026,10,4))
        self.conn.execute("INSERT INTO user_settings(user_id,timezone) VALUES(1,'unsupported')")
        self.conn.commit()
        with self.assertRaises(ValueError):
            schedule_due(self.conn,1,datetime(2026,10,4,8,tzinfo=timezone.utc))

    def test_automatic_failure_pauses_without_retry(self):
        self.seed_user()
        marker = Path(self.database).parent / "email_schedule.paused"
        with patch.object(pipeline,"DB_PATH",str(self.database)), \
                patch("scripts.run_local_email.send_digest",side_effect=requests.Timeout()) as send:
            with self.assertLogs("scripts.run_local_email",level="ERROR"):
                self.assertEqual(main(["--scheduled","--send"]),1)
            self.assertTrue(marker.exists())
            with self.assertLogs("scripts.run_local_email",level="ERROR"):
                self.assertEqual(main(["--scheduled","--send"]),1)
            send.assert_called_once()

    def test_automatic_success_clears_marker(self):
        self.seed_user()
        with patch.object(pipeline,"DB_PATH",str(self.database)), \
                patch("scripts.run_local_email.send_digest",return_value=0):
            self.assertEqual(main(["--scheduled","--send"]),0)
        self.assertFalse((Path(self.database).parent / "email_schedule.paused").exists())

    def test_preview_does_not_send_or_mark(self):
        self.seed_user()
        self.assertEqual(send_digest(self.conn), 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sent_log").fetchone()[0], 0)

    def test_send_acceptance_and_repeat_dedup(self):
        self.seed_user()
        pipeline.store_score(self.conn, 1, 1, sample_result())
        with patch.object(pipeline,"RESEND_API_KEY","test-placeholder"), \
                patch.object(pipeline,"DIGEST_FROM_EMAIL","jobs@example.test"), \
                patch.object(pipeline.requests,"post") as post:
            post.return_value.json.return_value={"id":"synthetic-email-id"}
            self.assertEqual(send_digest(self.conn,send=True),2)
            body=post.call_args.kwargs["json"]
            self.assertIn("Strong Match",body["html"])
            self.assertIn("Skills Match: 40",body["text"])
            self.assertIn("Idempotency-Key",post.call_args.kwargs["headers"])
            self.assertEqual(send_digest(self.conn,send=True),0)
            post.assert_called_once()
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sent_log").fetchone()[0],2)

    def test_failure_never_marks_sent(self):
        self.seed_user()
        for failure in (requests.Timeout(), requests.HTTPError()):
            with patch.object(pipeline,"deliver_email",side_effect=failure), self.assertRaises(type(failure)):
                send_digest(self.conn,send=True)
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sent_log").fetchone()[0],0)
            self.assertFalse(self.conn.in_transaction)

    def test_off_and_stale_are_skipped(self):
        self.seed_user()
        self.conn.execute("UPDATE scores SET is_stale=1")
        self.conn.commit()
        self.assertEqual(send_digest(self.conn,send=True),0)
        self.conn.execute("UPDATE scores SET is_stale=0")
        self.conn.execute("INSERT INTO user_settings(user_id,cadence) VALUES(1,'off')")
        self.conn.commit()
        self.assertEqual(send_digest(self.conn,send=True),0)

    def test_delivery_validation_and_stable_key(self):
        self.seed_user()
        user=self.conn.execute("SELECT * FROM users WHERE id=1").fetchone()
        matches=pipeline.get_unsent_top_matches(self.conn,1)
        with patch.object(pipeline,"RESEND_API_KEY","test-placeholder"), \
                patch.object(pipeline,"DIGEST_FROM_EMAIL","jobs@example.test"), \
                patch.object(pipeline.requests,"post") as post:
            post.return_value.json.return_value={}
            with self.assertRaises(ValueError):
                pipeline.deliver_email(user,matches)
            first=post.call_args.kwargs["headers"]["Idempotency-Key"]
            post.return_value.json.return_value={"id":"synthetic-email-id"}
            pipeline.deliver_email(user,matches)
            self.assertEqual(first,post.call_args.kwargs["headers"]["Idempotency-Key"])
            post.reset_mock()
            with patch.object(pipeline,"DIGEST_FROM_EMAIL","invalid"),self.assertRaises(ValueError):
                pipeline.deliver_email(user,matches)
            with self.assertRaises(ValueError):
                pipeline.deliver_email(user,[])
            post.assert_not_called()
