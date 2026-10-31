#!/usr/bin/env python3
"""Security and API regressions against temporary state; no external requests."""
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import unittest
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from xml.etree import ElementTree
from html import unescape
from contextlib import closing
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
SCRATCH = tempfile.TemporaryDirectory(prefix="researchd-web-")
from cryptography.fernet import Fernet
os.environ.update(DB_PATH=str(Path(SCRATCH.name) / "test.sqlite"),
                  APP_SECRET_KEY=Fernet.generate_key().decode(),
                  SECURE_COOKIE="0", API_TOKEN="synthetic-api", INGEST_TOKEN="synthetic-ingest")
import pyotp
from fastapi.testclient import TestClient
from web import app as web, security

API = {"Authorization": "Bearer synthetic-api"}
INGEST = {"Authorization": "Bearer synthetic-ingest"}
QUESTION = {"id": "regression", "label": "Research question",
            "concepts": [{"name": "topic", "terms": ["alpha"]}]}


class WebRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.owner = TestClient(web.app, follow_redirects=False)
        cls.credentials = {"username": "admin", "password": "synthetic-long-password"}
        cls.owner.post("/setup", data={**cls.credentials,
                                     "password2": cls.credentials["password"]})
        cls.owner.post("/login", data=cls.credentials)
        page = cls.owner.get("/2fa/enroll")
        assert '/static/vendor/qrcodejs/qrcode.min.js' in page.text
        assert '<script src="https://' not in page.text
        assert cls.owner.get('/static/vendor/qrcodejs/qrcode.min.js').status_code == 200
        cls.secret = re.search(r"<code>([A-Z2-7]+)</code>", page.text).group(1)
        enrollment = cls.owner.post("/2fa/enroll", data={"code": pyotp.TOTP(cls.secret).now()})
        cls.recovery_codes = re.findall(r"\b(?:[0-9a-f]{4}-){3}[0-9a-f]{4}\b", enrollment.text)
        assert cls.owner.get("/").status_code == 200

    def setUp(self):
        self.client = TestClient(web.app, follow_redirects=False)

    def test_password_only_session_cannot_reopen_enrollment(self):
        self.client.post("/login", data=self.credentials)
        with closing(sqlite3.connect(web.DB_PATH)) as con:
            before = con.execute("SELECT totp_secret,recovery_codes FROM auth WHERE id=1").fetchone()
        self.assertEqual(self.client.get("/").status_code, 307)
        page = self.client.get("/2fa/enroll")
        self.assertEqual(page.status_code, 303)
        self.assertEqual(page.headers["location"], "/2fa")
        self.assertNotIn(self.secret, page.text)
        self.assertEqual(page.headers["cache-control"], "no-store")
        self.assertEqual(page.headers["x-frame-options"], "DENY")
        posted = self.client.post("/2fa/enroll", data={"code": "000000"})
        self.assertEqual(posted.status_code, 409)
        self.assertNotIn(self.secret, posted.text)
        with closing(sqlite3.connect(web.DB_PATH)) as con:
            after = con.execute("SELECT totp_secret,recovery_codes FROM auth WHERE id=1").fetchone()
        self.assertEqual(before, after)
        self.assertEqual(self.client.get("/").status_code, 307)
        self.client.post("/2fa", data={"code": self.recovery_codes[0]})
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_totp_is_accepted_once_even_with_simultaneous_sessions(self):
        clients = [TestClient(web.app, follow_redirects=False) for _ in range(2)]
        for client in clients:
            client.post("/login", data=self.credentials)
        with closing(sqlite3.connect(web.DB_PATH)) as con:
            prior = con.execute("SELECT totp_last_step FROM auth WHERE id=1").fetchone()[0]
        with patch.object(security.time, "time", return_value=prior * 30 + 5):
            rejected = clients[0].post("/2fa", data={"code": pyotp.TOTP(self.secret).at(prior * 30 + 5)})
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(clients[0].get("/").status_code, 307)
        when = (prior + 1) * 30 + 5
        code = pyotp.TOTP(self.secret).at(when)
        with patch.object(security.time, "time", return_value=when), ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda client: client.post("/2fa", data={"code": code}), clients))
        self.assertEqual(sorted(r.status_code for r in results), [200, 303])
        self.assertEqual(sorted(client.get("/").status_code for client in clients), [200, 307])
        with closing(sqlite3.connect(web.DB_PATH)) as con:
            self.assertEqual(con.execute("SELECT totp_last_step FROM auth WHERE id=1").fetchone()[0], prior + 1)
        pending = next(client for client in clients if client.get("/").status_code == 307)
        next_time = when + 30
        with patch.object(security.time, "time", return_value=next_time):
            pending.post("/2fa", data={"code": pyotp.TOTP(self.secret).at(next_time)})
        self.assertEqual(pending.get("/").status_code, 200)

    def test_validation_errors_do_not_echo_credentials(self):
        self.client.post("/api/v1/questions", headers=API, json=QUESTION)
        secret = "synthetic-secret-never-echo-this"
        cases = [("/api/v1/questions", {**QUESTION, "from_days_back": 0,
                                       "api_keys": {"pubmed": secret}}),
                 ("/api/v1/questions", {**QUESTION, "api_keys": {secret: 123}}),
                 ("/api/v1/questions", {**QUESTION, "api_keys": {secret: secret}}),
                 ("/api/v1/signal_config/regression", {"min_confidence": "bad", "api_keys": {"openfda": secret}}),
                 ("/api/v1/signal_config/regression", {"api_keys": {secret: 123}})]
        for url, payload in cases:
            with self.subTest(url=url, payload=payload):
                response = self.client.post(url, headers=API, json=payload)
                self.assertEqual(response.status_code, 422)
                self.assertNotIn(secret, response.text)
                for error in response.json()["detail"]:
                    self.assertEqual(set(error), {"loc", "type", "msg"})

    def test_enrolled_owner_cannot_replace_recovery_codes(self):
        self.assertEqual(self.owner.get("/2fa/enroll").headers["location"], "/")
        self.assertEqual(self.owner.post("/2fa/enroll",
                         data={"code": pyotp.TOTP(self.secret).now()}).status_code, 409)

    def test_invalid_question_is_rejected_before_mutation(self):
        self.assertEqual(self.client.post("/api/v1/questions", headers=API,
                                         json=QUESTION).status_code, 200)
        before = self.client.get("/api/v1/questions", headers=API).json()
        invalid = [{"id": "../unsafe"}, {"concepts": []}, {"from_days_back": 0},
                   {"sources": {"pubmed": "yes"}}, {"sources": {"typo": True}},
                   {"digest_mode": "typo"}, {"email_cron": "not a cron"},
                   {"email_enabled": True, "email_to": []},
                   {"email_to": ["user@example.org\r\nBcc: someone@example.org"]}]
        for change in invalid:
            with self.subTest(change=change):
                response = self.client.post("/api/v1/questions", headers=API,
                                            json={**QUESTION, **change})
                self.assertEqual(response.status_code, 422)
        stored = self.client.get("/api/v1/questions", headers=API).json()
        self.assertEqual(stored, before)
        self.assertEqual(next(q for q in stored if q["id"] == "regression")["from_days_back"], 7)

    def test_signal_settings_validate_urls_and_accept_patentsview_key(self):
        self.client.post("/api/v1/questions", headers=API, json=QUESTION)
        url = "/api/v1/signal_config/regression"
        bad = {"scrape_targets": [{"name": "local", "url": "file:///etc/passwd", "type": "rss"}]}
        self.assertEqual(self.client.post(url, headers=API, json=bad).status_code, 422)
        for bad in [{"scrape_targets": ["https://example.org/feed"]},
                    {"scrape_targets": [{"url": "https://example.org/feed", "type": "rss", "name": []}]},
                    {"scrape_targets": [{"url": "https://example.org/feed", "type": "html", "selector": 2}]},
                    {"sources": {"feeds": "yes"}}, {"feed_categories": {"unknown": True}}]:
            with self.subTest(bad=bad):
                self.assertEqual(self.client.post(url, headers=API, json=bad).status_code, 422)
        self.assertEqual(self.client.post(url, headers=API,
            json={"sources": {"patentsview": True}, "api_keys": {"patentsview": "synthetic-key"}}).status_code, 200)
        config = self.client.get(url, headers=API)
        self.assertTrue(config.json()["api_key_present"]["patentsview"])
        self.assertNotIn("synthetic-key", config.text)

    def test_changed_signal_preserves_original_and_identical_retry_is_duplicate(self):
        self.client.post("/api/v1/questions", headers=API, json=QUESTION)
        signal = {"question_id": "regression", "signal_type": "regulatory",
                  "source": "regulator", "ext_id": "label1", "title": "First label",
                  "signal_date": "20261029", "extra": {"a": 1, "b": 2}}
        def ingest(value):
            return self.client.post("/api/ingest", headers=INGEST,
                                    json={"signals": [value]}).json()["signals"]
        self.assertEqual(ingest(signal)["inserted"], 1)
        changed = {**signal, "title": "Changed label", "signal_date": "2026-10-30"}
        self.assertEqual(ingest(changed)["inserted"], 1)
        self.assertEqual(ingest({**changed, "score": 3, "extra": {"b": 2, "a": 1}})["duplicates"], 1)
        self.assertEqual(ingest(signal)["duplicates"], 1)
        rows = self.client.get("/api/v1/signals", headers=API,
                               params={"question_id": "regression"}).json()["signals"]
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["signal_date"] for r in rows}, {"2026-10-29", "2026-10-30"})
        self.assertEqual({r["emailed"] for r in rows}, {0})

    def test_authentication_attempt_budget_expires(self):
        limiter = security.AttemptLimiter(limit=2, window_seconds=60)
        with patch.object(security.time, "monotonic", return_value=100):
            self.assertEqual(limiter.retry_after("client"), 0)
            self.assertEqual(limiter.retry_after("client"), 0)
            self.assertEqual(limiter.retry_after("client"), 60)
        with patch.object(security.time, "monotonic", return_value=160):
            self.assertEqual(limiter.retry_after("client"), 0)

    def test_browser_writes_reject_cross_origin_without_breaking_machine_api(self):
        for headers in ({"Origin": "https://evil.invalid"}, {"Origin": "null"},
                        {"Referer": "http://testserver.evil.invalid/form"},
                        {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.owner.post("/logout", headers=headers).status_code, 403)
                self.assertEqual(self.client.post("/setup", headers=headers,
                    data={**self.credentials, "password2": self.credentials["password"]}).status_code, 403)
        self.assertEqual(self.owner.get("/").status_code, 200)
        self.assertEqual(self.client.post("/login", headers={"Origin": "http://testserver"},
                                         data=self.credentials).status_code, 303)
        self.assertEqual(self.client.post("/api/v1/questions", headers={**API, "Origin": "null"},
                                         json=QUESTION).status_code, 200)

    def test_feeds_remain_xml_and_descriptions_remain_text(self):
        self.client.post("/api/v1/questions", headers=API,
                         json={**QUESTION, "id": "hostile", "label": "A\u0001 & B"})
        bad = "<img src=x onerror=alert(1)> & text\u0001"
        self.client.post("/api/ingest", headers=INGEST, json={
            "records": [{"question_id": "hostile", "title": bad, "abstract": bad,
                         "url": "javascript:alert(1)"}],
            "signals": [{"question_id": "hostile", "title": bad, "summary": bad,
                         "signal_type": "news", "url": "data:text/html,unsafe"}]})
        for prefix in ("feed", "signals"):
            response = self.client.get(f"/{prefix}/hostile.xml")
            self.assertEqual(response.status_code, 200)
            root = ElementTree.fromstring(response.text)
            item = root.find("channel/item")
            self.assertEqual(item.findtext("title"), bad.replace("\u0001", ""))
            self.assertEqual(item.findtext("link"), "")
            description = item.findtext("description")
            self.assertNotIn("<img", description)
            self.assertIn(bad.replace("\u0001", ""), unescape(description))
        self.assertEqual(web._feed_date("2026-10-31T15:30:00+02:00"),
                         "Sat, 31 Oct 2026 13:30:00 +0000")

    def test_concurrent_key_additions_preserve_both_secrets(self):
        self.client.post("/api/v1/questions", headers=API, json={**QUESTION, "id": "keys"})
        original = security.encrypt_secret
        for signal in (False, True):
            entered = Event()
            def encrypt(value):
                if value == "first":
                    entered.set()
                    time.sleep(0.15)  # second writer arrives during first key merge
                return original(value)
            def save(key, value):
                if signal:
                    web.upsert_signal_config("keys", web.SignalConfig(api_keys={key: value}), user="api")
                else:
                    web.upsert_question(web.Question(**{**QUESTION, "id": "keys", "api_keys": {key: value}}), user="api")
            keys = ("openfda", "patentsview") if signal else ("pubmed", "openalex")
            with patch.object(security, "encrypt_secret", side_effect=encrypt), ThreadPoolExecutor(2) as pool:
                first = pool.submit(save, keys[0], "first")
                self.assertTrue(entered.wait(2))
                second = pool.submit(save, keys[1], "second")
                first.result(); second.result()
            with closing(sqlite3.connect(web.DB_PATH)) as con:
                table, column = ("signal_config", "question_id") if signal else ("questions", "id")
                encrypted = con.execute(f"SELECT api_keys_enc FROM {table} WHERE {column}='keys'").fetchone()[0]
            self.assertEqual(security.decrypt_keymap(encrypted), {keys[0]: "first", keys[1]: "second"})

    def test_tied_pages_have_stable_unique_order(self):
        self.client.post("/api/v1/questions", headers=API, json={**QUESTION, "id": "paging"})
        with closing(sqlite3.connect(web.DB_PATH)) as con:
            for key in ("c", "b", "a"):
                con.execute("INSERT INTO records(dedup_key,question_id,title,first_seen,score) "
                            "VALUES(?,'paging','Paper','2026-10-30 12:00:00',1)", (key,))
                con.execute("INSERT INTO signals(dedup_key,question_id,title,signal_type,signal_date,score) "
                            "VALUES(?,'paging','Signal','regulatory','2026-10-30',1)", (key,))
            con.commit()
        for kind in ("records", "signals"):
            keys = []
            for offset in range(3):
                response = self.client.get(f"/api/v1/{kind}", headers=API,
                    params={"question_id": "paging", "limit": 1, "offset": offset})
                keys.extend(row["dedup_key"] for row in response.json()[kind])
            self.assertEqual(keys, ["a", "b", "c"])

    def test_unicode_titles_keep_distinct_scientific_identities(self):
        self.client.post("/api/v1/questions", headers=API, json={**QUESTION, "id": "unicode"})
        records = [{"question_id": "unicode", "title": title, "pub_date": "2026-10-30"}
                   for title in ("α receptor", "β receptor", "β   receptor")]
        result = self.client.post("/api/ingest", headers=INGEST, json={"records": records})
        self.assertEqual(result.json()["records"], {"inserted": 2, "duplicates": 1})
        self.assertEqual(web._record_dedup_key("", "ASCII receptor", "2026"),
                         "ti:ascii receptor|2026")
        self.assertEqual(web._record_dedup_key("10.123/ABC", "α receptor", "2026"),
                         "doi:10.123/abc")
        for title in ("", "   ", "!!!", "\u00a0"):
            response = self.client.post("/api/ingest", headers=INGEST,
                json={"records": [{"question_id": "unicode", "title": title}]})
            self.assertEqual(response.status_code, 422)
        response = self.client.post("/api/ingest", headers=INGEST,
            json={"records": [{"question_id": "unicode", "title": "", "doi": "10.123/no-title"}]})
        self.assertEqual(response.json()["records"]["inserted"], 1)

    def test_writer_parity_dates_identifiers_and_json_types(self):
        self.client.post("/api/v1/questions", headers=API, json={**QUESTION, "id": "parity"})
        records = [{"question_id": "parity", "title": f"Paper {i}", "pub_date": day}
                   for i, day in enumerate(("20261030", "2026-10-30T15:30:00+02:00", "2026-10", "2026"))]
        result = self.client.post("/api/ingest", headers=INGEST, json={"records": records})
        self.assertEqual(result.json()["records"]["inserted"], 4)
        result = self.client.get("/api/v1/records", headers=API,
                                 params={"question_id": "parity", "until": "2026-10-30"})
        self.assertEqual(result.json()["total"], 4)
        self.assertEqual({r["pub_date"] for r in result.json()["records"]},
                         {"2026-10-30", "2026-10-01", "2026-01-01"})
        for invalid in ("20261301", "2026-10-30junk", "not-a-date"):
            result = self.client.post("/api/ingest", headers=INGEST,
                                      json={"records": [{**records[0], "pub_date": invalid}]})
            self.assertEqual(result.status_code, 422)
        signal = {"question_id": "parity", "title": "Signal", "source": "press",
                  "signal_type": "regulatory", "url": "https://example.org/signal",
                  "extra": {"nested": [{"flag": True}]}}
        def ingest(value):
            return self.client.post("/api/ingest", headers=INGEST,
                                    json={"signals": [value]}).json()["signals"]
        self.assertEqual(ingest(signal)["inserted"], 1)
        self.assertEqual(ingest({**signal, "ext_id": signal["url"]})["duplicates"], 1)
        self.assertEqual(ingest({**signal, "extra": {"nested": [{"flag": 1}]}})["inserted"], 1)
        self.assertEqual(ingest({**signal, "extra": {"nested": [{"flag": 1.0}]}})["duplicates"], 1)

    def test_zotero_and_file_ingest_queue_is_atomic_and_not_replayed(self):
        self.client.post("/api/v1/questions", headers=API,
                         json={**QUESTION, "id": "queued", "delivery": ["file", "zotero"]})
        record = {"question_id": "queued", "title": "Queued paper", "doi": "10.123/queued"}
        for expected in ({"inserted": 1, "duplicates": 0}, {"inserted": 0, "duplicates": 1}):
            result = self.client.post("/api/ingest", headers=INGEST, json={"records": [record]})
            self.assertEqual(result.json()["records"], expected)
        with closing(sqlite3.connect(web.DB_PATH)) as con:
            queued = con.execute("SELECT channel,state FROM delivery_outbox "
                                 "WHERE question_id='queued' ORDER BY channel").fetchall()
            self.assertEqual(queued, [("file", "pending"), ("zotero", "pending")])
            con.execute("CREATE TRIGGER fail_test_queue BEFORE INSERT ON delivery_outbox "
                        "BEGIN SELECT RAISE(ABORT,'synthetic queue failure'); END")
            con.commit()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                self.client.post("/api/ingest", headers=INGEST,
                                 json={"records": [{**record, "doi": "10.123/rollback"}]})
        finally:
            with closing(sqlite3.connect(web.DB_PATH)) as con:
                con.execute("DROP TRIGGER fail_test_queue")
                con.commit()
                self.assertEqual(con.execute("SELECT COUNT(*) FROM records WHERE question_id='queued'").fetchone()[0], 1)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM delivery_outbox WHERE question_id='queued'").fetchone()[0], 2)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        SCRATCH.cleanup()
