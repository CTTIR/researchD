#!/usr/bin/env python3
"""Email failure, rendering, and calendar regressions. All SMTP calls are mocked."""
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from web import scheduler as scheduler


class PartialSMTP:
    refused = {"refused@example.invalid": (550, b"synthetic refusal")}
    def __init__(self, *args, **kwargs):
        self.timeout = kwargs.get("timeout")
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def sendmail(self, *args): return self.refused


class SchedulerRegression(unittest.TestCase):
    def test_new_midnight_schedule_runs_today_and_not_again_after_success(self):
        now = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
        self.assertTrue(scheduler.is_due("0 0 * * *", None, now))
        self.assertFalse(scheduler.is_due("0 0 * * *", now.isoformat(), now))
        self.assertTrue(scheduler.is_due("0 0 * * *", now.isoformat(),
                                        datetime(2026, 10, 31, 0, tzinfo=timezone.utc)))
        self.assertFalse(scheduler.is_due("0 23 * * *", None, now))

    def test_schedule_retains_named_timezone_across_dst_transition(self):
        zone = ZoneInfo("Europe/Berlin")
        last = datetime(2026, 10, 24, 7, tzinfo=zone)
        self.assertFalse(scheduler.is_due("0 7 * * *", last.isoformat(),
                                         datetime(2026, 10, 25, 6, 59, tzinfo=zone)))
        self.assertTrue(scheduler.is_due("0 7 * * *", last.isoformat(),
                                        datetime(2026, 10, 25, 7, 1, tzinfo=zone)))
        with patch.dict(os.environ, {"TZ": "Europe/Berlin"}):
            self.assertEqual(scheduler.now_local().tzinfo.key, "Europe/Berlin")

    def test_external_metadata_is_text_and_executable_links_are_not_rendered(self):
        record = {"study_type": "rct", "is_preprint": 0, "source": "<b>source</b>",
                  "url": "javascript:alert(1)", "title": "<img src=x onerror=alert(1)>",
                  "authors": "A&B", "pub_date": "2026-10-30"}
        html = scheduler.render_html("<script>label</script>", [record])
        self.assertNotIn("<img", html)
        self.assertNotIn("<script", html)
        self.assertNotIn("javascript:", html)
        self.assertIn("&lt;img", html)
        self.assertIn("A&amp;B", html)
        record["url"] = 'https://example.org/?q=" onclick="alert(1)'
        html = scheduler.render_html("Question", [record])
        self.assertIn("&quot;", html)
        self.assertNotIn('href="https://example.org/?q="', html)
        signal = {"signal_type": "regulatory", "confidence": "reported",
                  "title": "<img src=x>", "url": "data:text/html,unsafe",
                  "org": "<b>Org</b>", "signal_date": "2026-10-30"}
        html = scheduler.render_signals_html([signal])
        self.assertNotIn("<img", html)
        self.assertNotIn("data:text", html)
        self.assertIn("&lt;b&gt;Org", html)
        record["study_type"] = "<b>new-study-type</b>"
        signal["signal_type"] = "<b>new-signal-type</b>"
        self.assertIn("&lt;img", scheduler.render_html("Question", [record]))
        self.assertIn("&lt;img", scheduler.render_signals_html([signal]))
        self.assertNotIn("<b>", scheduler.render_signals_html([signal]))

    def test_partial_recipient_refusal_keeps_records_pending(self):
        with tempfile.TemporaryDirectory(prefix="researchd-scheduler-") as tmp:
            path = str(Path(tmp) / "state.sqlite")
            con = sqlite3.connect(path)
            con.executescript((ROOT / "db/schema.sql").read_text())
            con.execute("INSERT INTO questions(id,label,email_enabled,email_cron,email_to) "
                        "VALUES('q','Q',1,'0 0 * * *',?)",
                        ('["accepted@example.invalid","refused@example.invalid"]',))
            con.execute("INSERT INTO records(dedup_key,question_id,title,study_type) "
                        "VALUES('r','q','Paper','rct')")
            con.commit()
            with patch.multiple(scheduler, DB_PATH=path, SMTP_HOST="smtp.invalid",
                                SMTP_TLS="none", SMTP_USER="", SMTP_FROM="sender@example.invalid"), \
                    patch.object(scheduler.smtplib, "SMTP", PartialSMTP), \
                    patch.object(scheduler, "now_local", return_value=datetime(2026,10,30,12,tzinfo=timezone.utc)):
                self.assertFalse(scheduler.run_once())
                self.assertEqual(con.execute("SELECT emailed FROM records").fetchone()[0], 0)
                self.assertIsNone(con.execute("SELECT email_last_sent FROM questions").fetchone()[0])
                with patch.object(PartialSMTP, "refused", {}):
                    self.assertTrue(scheduler.run_once())
                self.assertEqual(con.execute("SELECT emailed FROM records").fetchone()[0], 1)
                self.assertIsNotNone(con.execute("SELECT email_last_sent FROM questions").fetchone()[0])
            con.close()

    def test_corrupt_question_does_not_block_other_digest_or_alert_batches(self):
        with tempfile.TemporaryDirectory(prefix="researchd-scheduler-") as tmp:
            path = str(Path(tmp) / "state.sqlite")
            con = sqlite3.connect(path)
            con.executescript((ROOT / "db/schema.sql").read_text())
            for qid, recipients in [("broken", "not-json"), ("working", '["reader@example.invalid"]')]:
                con.execute("INSERT INTO questions(id,label,email_enabled,email_cron,email_to) "
                            "VALUES(?,?,1,'0 0 * * *',?)", (qid, qid, recipients))
                con.execute("INSERT INTO records(dedup_key,question_id,title,study_type) "
                            "VALUES(?,?,'Paper','rct')", (qid, qid))
                con.execute("INSERT INTO signal_config(question_id,instant_alerts) VALUES(?,1)", (qid,))
                con.execute("INSERT INTO signals(dedup_key,question_id,title,signal_type,confidence) "
                            "VALUES(?,?,'Signal','regulatory','authoritative')", (qid, qid))
            con.commit()
            with patch.multiple(scheduler, DB_PATH=path), \
                    patch.object(scheduler, "send_email", return_value=True) as send, \
                    patch.object(scheduler, "now_local", return_value=datetime(2026,10,30,12,tzinfo=timezone.utc)):
                self.assertFalse(scheduler.run_alerts())
                self.assertEqual(send.call_count, 1)
                self.assertFalse(scheduler.run_once())
                self.assertEqual(send.call_count, 2)
            for table in ("records", "signals"):
                pending = con.execute(f"SELECT question_id FROM {table} WHERE emailed=0").fetchall()
                self.assertEqual(pending, [("broken",)])
            con.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
