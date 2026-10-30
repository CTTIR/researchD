#!/usr/bin/env python3
"""Phase 8 — operations: restart continuity, observability, backup/restore,
retention, failure isolation, and documentation completeness."""
import importlib.metadata as md, json, os, pathlib, re, shutil, sqlite3, subprocess, sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
TMP = ROOT / "dev" / "state" / "tmp"; TMP.mkdir(parents=True, exist_ok=True)
DB = TMP / "p8.sqlite"
for suf in ("", "-wal", "-shm"):
    pathlib.Path(str(DB) + suf).unlink(missing_ok=True)

from cryptography.fernet import Fernet
KEY = Fernet.generate_key().decode()
os.environ.update(DB_PATH=str(DB), APP_SECRET_KEY=KEY, SECURE_COOKIE="0",
                  API_TOKEN="p8-token", SMTP_HOST="smtp.invalid", SMTP_FROM="p@x",
                  SMTP_USER="p@x", EMAIL_PW="pw", SMTP_TLS="starttls")

import pyotp
from fastapi.testclient import TestClient

fails = []
def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if (detail and not ok) else ""))
    if not ok: fails.append(name)

# --------------------------------------------------- 1. restart continuity
print("1. a down/up cycle preserves everything that matters")
import web.app as appmod
c = TestClient(appmod.app, follow_redirects=False)
c.post("/setup", data={"username": "admin", "password": "correct-horse-battery",
                       "password2": "correct-horse-battery"})
c.post("/login", data={"username": "admin", "password": "correct-horse-battery"})
c.get("/2fa/enroll")
secret = sqlite3.connect(DB).execute("SELECT totp_secret FROM auth WHERE id=1").fetchone()[0]
c.post("/2fa/enroll", data={"code": pyotp.TOTP(secret).now()})
COOKIE = c.cookies.get("rf_session")
c.post("/api/questions", json={
    "id": "q1", "label": "Q1", "concepts": [{"name": "a", "terms": ["alpha"], "mesh": []}],
    "api_keys": {"pubmed": "KEY-SURVIVES-RESTART"},
    "email_enabled": True, "email_cron": "0 7 * * *", "email_to": ["x@example.org"]})
con = sqlite3.connect(DB)
con.execute("INSERT INTO records(dedup_key,question_id,title,study_type,pub_date,score,emailed)"
            " VALUES('r1','q1','T','rct','2026-08-01',5.0,0)")
FIXED_NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
LAST_SENT = (FIXED_NOW - timedelta(hours=1)).isoformat()
con.execute("UPDATE questions SET email_last_sent=? WHERE id='q1'", (LAST_SENT,))
con.commit(); con.close()

# "down": drop every module that holds a connection, then re-import ("up")
for mod in [m for m in list(sys.modules) if m.startswith("web.")]:
    del sys.modules[mod]
import web.app as appmod2
from web import scheduler as sch2
sch2.now_local = lambda: FIXED_NOW
c2 = TestClient(appmod2.app, follow_redirects=False)

check("the database file survived the cycle", DB.exists())
check("the question survived", sqlite3.connect(DB).execute(
    "SELECT COUNT(*) FROM questions WHERE id='q1'").fetchone()[0] == 1)
check("its records survived", sqlite3.connect(DB).execute(
    "SELECT COUNT(*) FROM records WHERE question_id='q1'").fetchone()[0] == 1)
check("the encrypted API key survived and still decrypts",
      __import__("web.security", fromlist=["x"]).decrypt_secret(
          json.loads(sqlite3.connect(DB).execute(
              "SELECT api_keys_enc FROM questions WHERE id='q1'").fetchone()[0])["pubmed"])
      == "KEY-SURVIVES-RESTART")
check("the admin account survived", sqlite3.connect(DB).execute(
    "SELECT username FROM auth WHERE id=1").fetchone()[0] == "admin")
resumed = TestClient(appmod2.app, follow_redirects=False, cookies={"rf_session": COOKIE})
check("an existing session still works after the restart (sessions are in the DB)",
      resumed.get("/").status_code == 200)
check("email_last_sent survived", sqlite3.connect(DB).execute(
    "SELECT email_last_sent FROM questions WHERE id='q1'").fetchone()[0] == LAST_SENT)

SENT = []
sch2.send_email = lambda to, s, h: (SENT.append((to, s)), True)[1]
sch2.run_once()
check("the scheduler does NOT re-blast on restart (it respects email_last_sent)",
      len(SENT) == 0, str(SENT))
con = sqlite3.connect(DB)
con.execute("UPDATE questions SET email_last_sent=? WHERE id='q1'",
            ((FIXED_NOW - timedelta(days=2)).isoformat(),))
con.commit(); con.close()
sch2.run_once()
check("...and does send once genuinely due again", len(SENT) == 1, str(SENT))

check("all three services declare restart: unless-stopped",
      (ROOT / "docker-compose.yml").read_text().count("restart: unless-stopped") == 3)
check("the fetch schedule is rendered from CRON_SCHEDULE at container start",
      "__SCHEDULE__" in (ROOT / "crontab.template").read_text()
      and "CRON_SCHEDULE" in (ROOT / "entrypoint.sh").read_text())
check("db, digests and config are bind-mounted so they outlive the container",
      all(v in (ROOT / "docker-compose.yml").read_text()
          for v in ("./db:/app/db", "./digests:/app/digests", "./config:/app/config")))

# ------------------------------------------------------- 2. observability
print("\n2. observability")
sch2.heartbeat(ok=True, detail="unit test")
hb = {r["service"]: r for r in sqlite3.connect(DB).execute(
    "SELECT service,last_run,last_ok,last_detail FROM service_heartbeat")
    if True} if False else None
con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
rows = {r["service"]: dict(r) for r in con.execute("SELECT * FROM service_heartbeat")}
con.close()
check("the scheduler writes a service heartbeat", "scheduler" in rows, str(list(rows)))
check("the heartbeat records both last_run and last_ok",
      rows.get("scheduler", {}).get("last_run") and rows.get("scheduler", {}).get("last_ok"))
r = c2.get("/api/v1/heartbeat", headers={"Authorization": "Bearer p8-token"})
check("the heartbeat is exposed on /api/v1", r.status_code == 200, str(r.status_code))
check("...and reports staleness", "stale" in json.dumps(r.json()))
check("the dashboard heartbeat endpoint needs a session",
      TestClient(appmod2.app, follow_redirects=False)
      .get("/api/heartbeat").status_code in (307, 401, 403))

runsrc = (ROOT / "R" / "run_all.R").read_text()
check("the engine writes a heartbeat at the end of a run",
      'heartbeat(con, "pipeline"' in runsrc)
check("a failed engine run does not advance last_ok",
      "ok = (n_failed == 0L)" in runsrc)
check("the engine logs per-source counts to fetch_log",
      'log_fetch(con, q$id, "lit"' in runsrc)
check("the engine exits non-zero when a question failed",
      "quit(status = 1L)" in runsrc)
schsrc = (ROOT / "web" / "scheduler.py").read_text()
check("the scheduler logs its interval, timezone and SMTP target at startup",
      "tz=" in schsrc and "smtp=" in schsrc)
check("the scheduler warns loudly when SMTP_HOST is unset",
      "SMTP_HOST is unset" in schsrc)
check("the scheduler logs each dispatch with question and recipient counts",
      "recipient(s)" in schsrc)

# --------------------------------------------------- 3. backup and restore
print("\n3. backup / restore, exercised")
ops = TMP / "ops8"; shutil.rmtree(ops, ignore_errors=True)
(ops / "backups").mkdir(parents=True); (ops / "digests").mkdir(parents=True)
live = ops / "live.sqlite"
shutil.copy(DB, live)
r = subprocess.run([str(ROOT / "scripts" / "backup.sh"), str(live), str(ops / "backups"), "3"],
                   capture_output=True, text=True, cwd=ROOT)
check("backup.sh succeeds", r.returncode == 0, (r.stdout + r.stderr)[-200:])
check("...and verifies integrity before trusting the snapshot",
      "integrity_check=ok" in r.stdout, r.stdout)
snaps = list((ops / "backups").glob("*.sqlite"))
check("a snapshot was written", len(snaps) == 1, str(snaps))
check("backup.sh does not require the sqlite3 CLI",
      "python3" in (ROOT / "scripts" / "backup.sh").read_text())
check("backup.sh reminds you to back up APP_SECRET_KEY separately",
      "APP_SECRET_KEY" in (ROOT / "scripts" / "backup.sh").read_text())

con = sqlite3.connect(live); con.execute("DELETE FROM records"); con.commit(); con.close()
r = subprocess.run([str(ROOT / "scripts" / "restore.sh"), str(snaps[0]), str(live)],
                   capture_output=True, text=True, cwd=ROOT)
check("restore.sh succeeds", r.returncode == 0, (r.stdout + r.stderr)[-200:])
check("the restored database has the records back",
      sqlite3.connect(live).execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1)
check("restore.sh moves the old database aside rather than deleting it",
      len(list(ops.glob("live.sqlite.replaced-*"))) == 1)
check("restore.sh clears a stale -wal so it cannot be applied to the new file",
      '-wal' in (ROOT / "scripts" / "restore.sh").read_text())

# ------------------------------------------------------------ 4. retention
print("\n4. retention")
con = sqlite3.connect(live)
for i in range(20):
    con.execute("INSERT OR REPLACE INTO records(dedup_key,question_id,title,first_seen)"
                " VALUES(?,?,?,?)", (f"old{i}", "q1", "T",
                "2020-01-01 00:00:00" if i < 12 else "2026-08-01 00:00:00"))
con.commit(); con.close()
for i in range(40):
    (ops / "digests" / f"d-{i:03d}.html").write_text("x")
r = subprocess.run([str(ROOT / "scripts" / "prune.sh"), str(live), str(ops / "digests"), "24", "10"],
                   capture_output=True, text=True, cwd=ROOT)
check("prune.sh succeeds", r.returncode == 0, (r.stdout + r.stderr)[-200:])
con = sqlite3.connect(live)
check("old records were pruned", con.execute(
    "SELECT COUNT(*) FROM records WHERE first_seen < '2021'").fetchone()[0] == 0)
n_recent = con.execute("SELECT COUNT(*) FROM records WHERE first_seen > '2026'").fetchone()[0]
check("recent records were kept", n_recent >= 8, f"{n_recent} recent rows left")
check("configuration tables are never pruned",
      con.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 1
      and con.execute("SELECT COUNT(*) FROM auth").fetchone()[0] == 1)
con.close()
check("digests were rotated to the newest 10",
      len(list((ops / "digests").glob("*.html"))) == 10,
      str(len(list((ops / "digests").glob("*.html")))))
check("the WAL is checkpointed by the engine at the end of a run",
      "wal_checkpoint(TRUNCATE)" in runsrc)
check("...and by prune.sh", "wal_checkpoint(TRUNCATE)" in (ROOT / "scripts" / "prune.sh").read_text())

# ---------------------------------------------------- 5. failure isolation
print("\n5. failure isolation")
check("a fetcher error degrades only that source (.safe wrapper)",
      (ROOT / "R" / "02_fetch.R").read_text().count(".safe(fetch_") == 7)
check("one question failing does not abort the run", "n_failed <<- n_failed + 1L" in runsrc)
check("an unloadable question is skipped, not fatal",
      "skipping unloadable question" in (ROOT / "R" / "07_db_questions.R").read_text())
con = sqlite3.connect(DB)
con.execute("INSERT INTO questions(id,label,email_enabled,email_cron,email_to,email_last_sent)"
            " VALUES('failure','Failure',1,'* * * * *','[\"x@example.org\"]',?)",
            ((FIXED_NOW-timedelta(days=1)).isoformat(),))
con.execute("INSERT INTO records(dedup_key,question_id,title,study_type,emailed)"
            " VALUES('pending','failure','Pending','unclassified',0)")
con.commit(); con.close()
sch2.send_email = lambda *args: False
sch2.run_once()
con = sqlite3.connect(DB)
check("a failed send leaves the actual record pending",
      con.execute("SELECT emailed FROM records WHERE dedup_key='pending'").fetchone()[0] == 0)
con.close()
sch2.send_email = lambda *args: True
sch2.run_once()
con = sqlite3.connect(DB)
check("a successful retry acknowledges the actual record",
      con.execute("SELECT emailed FROM records WHERE dedup_key='pending'").fetchone()[0] == 1)
con.close()
check("a scheduler loop error is caught so the service stays up",
      "loop error" in schsrc)

# ------------------------------------------------------- 6. config and docs
print("\n6. configuration and documentation")
compose = (ROOT / "docker-compose.yml").read_text()
envex = (ROOT / ".env.example").read_text()
refs = set(re.findall(r"\$\{([A-Z_0-9]+)", compose))
documented = set(re.findall(r"^([A-Z_0-9]+)=", envex, re.M))
check("every compose variable is documented in .env.example",
      not (refs - documented), str(sorted(refs - documented)))
check("nothing in .env.example is unused by compose",
      not (documented - refs), str(sorted(documented - refs)))
readme = (ROOT / "README.md").read_text()
for topic, needle in [
        ("the three services", "scheduler"),
        ("the researchR client", "researchR"),
        ("the first-run 2FA flow", "2FA"),
        ("both independent schedules", "CRON_SCHEDULE"),
        ("per-question email cadence", "email_cron"),
        ("backups", "backup.sh"),
        ("restore", "restore.sh"),
        ("retention", "prune.sh"),
        ("the API token", "API_TOKEN"),
        ("TLS reverse proxy", "reverse proxy"),
        ("APP_SECRET_KEY backup warning", "APP_SECRET_KEY")]:
    check(f"README covers {topic}", needle in readme, f"missing: {needle}")

# --------------------------------------------------- 7. dependency posture
print("\n7. dependency pins")
text = (ROOT / "Dockerfile.web").read_text() + (ROOT / "Dockerfile.scheduler").read_text()
specs = []
for manifest in ("requirements-web.txt", "requirements-scheduler.txt"):
    for line in (ROOT / manifest).read_text().splitlines():
        if line and not line.startswith(("#", "-")):
            from packaging.requirements import Requirement
            req = Requirement(line)
            specs.append((req.name, str(req.specifier)))
check("deployment dependencies are explicitly version-pinned", bool(specs) and
      all(spec.startswith("==") and "*" not in spec for _, spec in specs))
from packaging.specifiers import SpecifierSet
from packaging.version import Version
out = []
for name, spec in specs:
    try:
        if Version(md.version(name)) not in SpecifierSet(spec):
            out.append(f"{name} {md.version(name)} !~ {spec}")
    except md.PackageNotFoundError:
        out.append(f"{name} is missing")
check("installed dependency versions satisfy the deployment pins", not out, "; ".join(out))
check("no base image floats on :latest",
      not re.search(r"^FROM\s+\S+:latest", text + (ROOT / "Dockerfile").read_text(), re.M))

print()
if fails:
    print(f"FAILED: {len(fails)} check(s):"); [print("   -", f) for f in fails]; sys.exit(1)
print("ALL PHASE 8 CHECKS PASSED"); sys.exit(0)
