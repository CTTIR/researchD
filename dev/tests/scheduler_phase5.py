#!/usr/bin/env python3
"""Phase 5 — per-question email dispatch, driven against a seeded DB with
send_email mocked. SMTP delivery itself is host-side."""
import json, os, pathlib, sqlite3, sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
TMP = ROOT / "dev" / "state" / "tmp"; TMP.mkdir(parents=True, exist_ok=True)
DB = TMP / "p5.sqlite"
for suf in ("", "-wal", "-shm"):
    pathlib.Path(str(DB) + suf).unlink(missing_ok=True)
os.environ.update(DB_PATH=str(DB), SMTP_HOST="smtp.invalid", SMTP_FROM="p@x",
                  SMTP_USER="p@x", EMAIL_PW="pw", SMTP_TLS="starttls")

from web import scheduler as sch

fails = []
def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if (detail and not ok) else ""))
    if not ok: fails.append(name)

con = sqlite3.connect(DB)
con.executescript((ROOT / "db" / "schema.sql").read_text())
con.commit()

SENT = []
def fake_send(to_list, subject, html):
    SENT.append({"to": list(to_list), "subject": subject, "html": html})
    return True
def failing_send(to_list, subject, html):
    SENT.append({"to": list(to_list), "subject": subject, "html": html, "failed": True})
    return False
sch.send_email = fake_send

def seed_question(qid, cron="0 7 * * 1", to=("a@example.org",), last=None,
                  digest_mode="all", min_score=0.0, enabled=1, email_enabled=1):
    con.execute("""INSERT OR REPLACE INTO questions(id,label,enabled,email_enabled,
                     email_cron,email_to,email_last_sent,digest_mode,min_score)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (qid, f"Q {qid}", enabled, email_enabled, cron, json.dumps(list(to)),
                 last, digest_mode, min_score))
    con.execute("INSERT OR REPLACE INTO signal_config(question_id) VALUES(?)", (qid,))
    con.commit()

def seed_record(qid, key, study_type="rct", score=5.0, preprint=0, emailed=0):
    con.execute("""INSERT OR REPLACE INTO records(dedup_key,question_id,source,doi,title,
                     authors,pub_date,url,study_type,is_preprint,score,emailed)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (key, qid, "pubmed", "10.1/" + key, "Title " + key, "Author A",
                 "2026-08-01", "https://example.org/" + key, study_type, preprint,
                 score, emailed))
    con.commit()

# ------------------------------------------------------------------ is_due
print("1. is_due")
NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)      # a Wednesday
WEEKLY, DAILY, MONTHLY = "0 7 * * 1", "0 7 * * *", "0 7 1 * *"
iso = lambda dt: dt.isoformat()

check("weekly, last sent 8 days ago -> due",
      sch.is_due(WEEKLY, iso(NOW - timedelta(days=8)), NOW))
check("weekly, last sent 1 hour ago -> not due",
      not sch.is_due(WEEKLY, iso(NOW - timedelta(hours=1)), NOW))
# NOW is a Wednesday; 2 days ago is Monday 12:00, i.e. after that Monday's
# 07:00 fire point, so the next one is a week out.
check("weekly, last sent on the fire day but after it -> not due",
      not sch.is_due(WEEKLY, iso(NOW - timedelta(days=2)), NOW))
check("weekly, last sent the day before the fire day -> due",
      sch.is_due(WEEKLY, iso(NOW - timedelta(days=3)), NOW))
check("daily, last sent yesterday -> due",
      sch.is_due(DAILY, iso(NOW - timedelta(days=1)), NOW))
check("daily, last sent 10 minutes ago -> not due",
      not sch.is_due(DAILY, iso(NOW - timedelta(minutes=10)), NOW))
check("monthly, last sent 40 days ago -> due",
      sch.is_due(MONTHLY, iso(NOW - timedelta(days=40)), NOW))
check("monthly, last sent 3 days ago -> not due",
      not sch.is_due(MONTHLY, iso(NOW - timedelta(days=3)), NOW))
check("never sent, daily, past today's fire point -> due",
      sch.is_due(DAILY, None, NOW))
check("never sent, daily, before today's fire point -> not due",
      not sch.is_due(DAILY, None, NOW.replace(hour=3)))
check("never sent, weekly on a non-matching weekday -> not due",
      not sch.is_due(WEEKLY, None, NOW))
check("a malformed cron fails safe (not due, not an exception)",
      sch.is_due("not a cron", iso(NOW - timedelta(days=99)), NOW) is False)
check("an empty cron fails safe", sch.is_due("", None, NOW) is False)
check("an unparseable last_sent is treated as never-sent, not as an error",
      sch.is_due(DAILY, "garbage-timestamp", NOW) is True)

# --------------------------------------------------------------- send path
print("\n1b. cron fire points are wall-clock in the deployment's timezone")
from zoneinfo import ZoneInfo
BERLIN = ZoneInfo("Europe/Berlin")
# 06:30 Berlin is 04:30 UTC. A 06:00 daily cron has already fired locally but
# not yet in UTC, so this distinguishes the two readings.
now_b = datetime(2026, 8, 26, 6, 30, tzinfo=BERLIN)
last_b = datetime(2026, 8, 25, 12, 0, tzinfo=BERLIN)
check("a 06:00 daily cron is due at 06:30 local time",
      sch.is_due("0 6 * * *", iso(last_b), now_b))
check("...and is not yet due at 05:30 local time",
      not sch.is_due("0 6 * * *", iso(last_b), now_b.replace(hour=5, minute=30)))
# a last_sent stored in UTC (older rows) must still be compared correctly
check("a UTC-stored last_sent is converted before the fire points are walked",
      sch.is_due("0 6 * * *", iso(last_b.astimezone(timezone.utc)), now_b))
n = sch.now_local()
check("the scheduler's clock is timezone-aware, not naive", n.tzinfo is not None)
check("...and it is local time, not hard-coded UTC",
      n.utcoffset() == datetime.now().astimezone().utcoffset(), str(n.utcoffset()))

print("\n2. the send path")
seed_question("qa", cron=DAILY, to=("a@example.org", "b@example.org"),
              last=iso(NOW - timedelta(days=2)))
seed_record("qa", "r1", "rct", 5.0)
seed_record("qa", "r2", "systematic_review", 4.0)
seed_record("qa", "r3", "rct", 3.0, preprint=1)
SENT.clear(); sch.run_once()

check("exactly one email was sent", len(SENT) == 1, str(len(SENT)))
if SENT:
    m = SENT[0]
    check("sent to BOTH recipients", m["to"] == ["a@example.org", "b@example.org"], str(m["to"]))
    check("the subject carries the new-record count", "3 papers" in m["subject"], m["subject"])
    check("HTML is grouped by study type with the right headings",
          "Randomized controlled trials (2)" in m["html"]
          and "Systematic reviews & meta-analyses" in __import__("html").unescape(m["html"]),
          m["html"][:300])
    check("systematic reviews are rendered before RCTs (evidence order)",
          m["html"].index("Systematic reviews") < m["html"].index("Randomized controlled"))
    check("the preprint marker is shown", ">preprint<" in m["html"])
rows = con.execute("SELECT dedup_key,emailed FROM records WHERE question_id='qa'").fetchall()
check("every sent record is marked emailed=1", all(r[1] == 1 for r in rows), str(rows))
last = con.execute("SELECT email_last_sent FROM questions WHERE id='qa'").fetchone()[0]
check("email_last_sent advanced", bool(last), str(last))

print("\n3. no double-send")
SENT.clear(); sch.run_once()
check("running again immediately sends nothing", len(SENT) == 0, str(len(SENT)))
seed_record("qa", "r4", "rct", 5.0)
SENT.clear(); sch.run_once()
check("...and it is the cadence, not the emptiness, holding it back "
      "(a new record still waits for the next fire point)", len(SENT) == 0, str(len(SENT)))

print("\n4. empty but due")
con.execute("UPDATE questions SET email_last_sent=? WHERE id='qa'",
            (iso(datetime.now(timezone.utc) - timedelta(days=3)),))
con.execute("UPDATE records SET emailed=1 WHERE question_id='qa'"); con.commit()
before = con.execute("SELECT email_last_sent FROM questions WHERE id='qa'").fetchone()[0]
SENT.clear(); sch.run_once()
after = con.execute("SELECT email_last_sent FROM questions WHERE id='qa'").fetchone()[0]
check("a due question with nothing new sends no email", len(SENT) == 0, str(len(SENT)))
check("...but still advances last_sent, so it stops rechecking every tick",
      after != before, f"{before} -> {after}")

print("\n5. SMTP failure must not consume the records (ACCEPTANCE C2)")
seed_question("qf", cron="0 7 * * *", to=("c@example.org",),
              last=iso(datetime.now(timezone.utc) - timedelta(days=2)))
seed_record("qf", "f1", "rct", 5.0)
sch.send_email = failing_send
SENT.clear(); sch.run_once()
check("a failed send is attempted", len(SENT) == 1, str(len(SENT)))
em = con.execute("SELECT emailed FROM records WHERE dedup_key='f1'").fetchone()[0]
check("the record is NOT marked emailed after a failed send", em == 0, str(em))
lf = con.execute("SELECT email_last_sent FROM questions WHERE id='qf'").fetchone()[0]
check("last_sent is NOT advanced after a failed send",
      lf.startswith((datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")), str(lf))
sch.send_email = fake_send
SENT.clear(); sch.run_once()
check("the retry delivers it", len(SENT) == 1, str(len(SENT)))
em = con.execute("SELECT emailed FROM records WHERE dedup_key='f1'").fetchone()[0]
check("...and only then is it marked emailed (exactly once)", em == 1, str(em))

print("\n6. isolation between questions")
now = datetime.now(timezone.utc)
seed_question("q1", cron="0 7 * * *", to=("one@example.org",), last=iso(now - timedelta(days=2)))
seed_question("q2", cron="0 7 1 * *", to=("two@example.org",), last=iso(now - timedelta(days=2)))
seed_record("q1", "a1", "rct", 5.0)
seed_record("q2", "b1", "rct", 5.0)
SENT.clear(); sch.run_once()
recipients = [m["to"] for m in SENT]
check("only the due question mailed", recipients == [["one@example.org"]], str(recipients))
check("question 1's records never reached question 2's recipients",
      all("two@example.org" not in m["to"] for m in SENT))
if SENT:
    check("the email contains only its own question's records",
          "Title a1" in SENT[0]["html"] and "Title b1" not in SENT[0]["html"])
check("the not-due question's record is still unemailed",
      con.execute("SELECT emailed FROM records WHERE dedup_key='b1'").fetchone()[0] == 0)

print("\n7. the digest threshold gates email, but does not consume records")
seed_question("qt", cron="0 7 * * *", to=("t@example.org",),
              last=iso(now - timedelta(days=2)), digest_mode="threshold", min_score=2.0)
seed_record("qt", "hi", "rct", 5.0)
seed_record("qt", "lo", "rct", 0.5)
SENT.clear(); sch.run_once()
check("only above-threshold records are emailed",
      len(SENT) == 1 and "Title hi" in SENT[0]["html"] and "Title lo" not in SENT[0]["html"])
check("the below-threshold record is NOT marked emailed, so lowering the "
      "threshold later still delivers it",
      con.execute("SELECT emailed FROM records WHERE dedup_key='lo'").fetchone()[0] == 0)

print("\n8. SMTP transport modes")
src = (ROOT / "web" / "scheduler.py").read_text()
check("an implicit-SSL path exists", "SMTP_SSL" in src)
check("a STARTTLS path exists", 'SMTP_TLS == "starttls"' in src and "starttls(" in src)
check("plain SMTP is reachable when SMTP_TLS=none",
      'smtplib.SMTP(' in src and 'SMTP_TLS == "starttls"' in src)
check("SMTP_TLS is normalised to lower case", '.lower()' in src)
check("login is skipped when no SMTP_USER is set", "if SMTP_USER:" in src)
check("a send failure is caught and reported, never raised",
      "except Exception as e" in src and "send failed" in src)

print()
if fails:
    print(f"FAILED: {len(fails)} check(s):"); [print("   -", f) for f in fails]; sys.exit(1)
print("ALL PHASE 5 CHECKS PASSED"); sys.exit(0)
