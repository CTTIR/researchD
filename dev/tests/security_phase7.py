#!/usr/bin/env python3
"""Phase 7 — adversarial pass: try to break our own auth and leak our own keys."""
import ast, io, json, os, pathlib, re, sqlite3, subprocess, sys
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
TMP = ROOT / "dev" / "state" / "tmp"; TMP.mkdir(parents=True, exist_ok=True)
DB = TMP / "p7.sqlite"
for suf in ("", "-wal", "-shm"):
    pathlib.Path(str(DB) + suf).unlink(missing_ok=True)

from cryptography.fernet import Fernet
KEY = Fernet.generate_key().decode()
SECRET_KEY_VALUE = "SUPER-SECRET-NCBI-KEY-0123456789"
os.environ.update(DB_PATH=str(DB), APP_SECRET_KEY=KEY, SECURE_COOKIE="1",
                  BASE_URL="http://testserver", API_TOKEN="p7-token",
                  INGEST_TOKEN="p7-ingest", SMTP_HOST="smtp.invalid",
                  EMAIL_PW="SMTP-PASSWORD-SHOULD-NEVER-APPEAR")

import pyotp
from fastapi.testclient import TestClient
from web.app import app
from web import security as sec

fails = []
def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if (detail and not ok) else ""))
    if not ok: fails.append(name)

DENIED = (301, 302, 303, 307, 308, 401, 403)

# SECURE_COOKIE=1 is deliberately left on for this whole phase, so the client
# has to speak https or the browser would never return the session cookie --
# which is exactly the production posture we want to be testing against.
def client(**kw):
    kw.setdefault("follow_redirects", False)
    return TestClient(app, base_url="https://testserver", **kw)
def blocked(resp):
    return resp.status_code in DENIED

c = client()
c.post("/setup", data={"username": "admin", "password": "correct-horse-battery",
                       "password2": "correct-horse-battery"})
c.post("/login", data={"username": "admin", "password": "correct-horse-battery"})
c.get("/2fa/enroll")
secret = sqlite3.connect(DB).execute("SELECT totp_secret FROM auth WHERE id=1").fetchone()[0]
enroll = c.post("/2fa/enroll", data={"code": pyotp.TOTP(secret).now()})
CODES = re.findall(r"\b(?:[0-9a-f]{4}-){3}[0-9a-f]{4}\b", enroll.text)
GOOD_COOKIE = c.cookies.get("rf_session")

# ------------------------------------------------------------- 1. bypasses
print("1. auth bypass attempts (every one must be denied)")
PROTECTED = ["/", "/api/questions", "/api/source_health", "/api/signal_config/x"]

for path in PROTECTED:
    check(f"no cookie: {path}", blocked(client().get(path)))
for path in PROTECTED:
    cl = client(cookies={"rf_session": "forged-" + "a" * 40})
    check(f"forged cookie: {path}", blocked(cl.get(path)))

con = sqlite3.connect(DB)
con.execute("INSERT INTO sessions(token,expires_at,mfa_passed) VALUES('expired-token',?,1)",
            ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),))
con.execute("INSERT INTO sessions(token,expires_at,mfa_passed) VALUES('nomfa-token',?,0)",
            ((datetime.now(timezone.utc) + timedelta(hours=12)).isoformat(),))
con.commit(); con.close()
for path in PROTECTED:
    cl = client(cookies={"rf_session": "expired-token"})
    check(f"expired session: {path}", blocked(cl.get(path)))
for path in PROTECTED:
    cl = client(cookies={"rf_session": "nomfa-token"})
    r = cl.get(path)
    check(f"valid session that never passed MFA: {path}",
          blocked(r) and "/2fa" in r.headers.get("location", ""),
          f"{r.status_code} {r.headers.get('location')}")

check("a valid, MFA-passed session DOES reach the dashboard",
      c.get("/").status_code == 200)
c.post("/logout")
replay = client(cookies={"rf_session": GOOD_COOKIE})
for path in PROTECTED:
    check(f"replaying a logged-out token: {path}", blocked(replay.get(path)))
check("the logged-out token is gone from the sessions table",
      not sqlite3.connect(DB).execute(
          "SELECT 1 FROM sessions WHERE token=?", (GOOD_COOKIE,)).fetchone())

print("\n1b. writes are gated too, not just reads")
anon = client()
QB = {"id": "x", "label": "x", "concepts": [{"name": "a", "terms": ["b"], "mesh": []}]}
check("POST /api/questions with no session", blocked(anon.post("/api/questions", json=QB)))
check("DELETE /api/questions with no session", blocked(anon.delete("/api/questions/x")))
check("POST /api/signal_config with no session",
      blocked(anon.post("/api/signal_config/x", json={"sources": {}})))
check("POST /api/ingest with no bearer token",
      anon.post("/api/ingest", json={"records": []}).status_code == 401)
check("POST /api/ingest with the WRONG bearer token",
      anon.post("/api/ingest", json={"records": []},
                headers={"Authorization": "Bearer wrong"}).status_code == 401)
check("the ingest token is not accepted on /api/v1",
      anon.get("/api/v1/questions",
               headers={"Authorization": "Bearer p7-ingest"}).status_code == 401)
check("the api token is not accepted on /api/ingest",
      anon.post("/api/ingest", json={"records": []},
                headers={"Authorization": "Bearer p7-token"}).status_code == 401)
check("a session cookie does not open /api/v1",
      client(cookies={"rf_session": "nomfa-token"}).get("/api/v1/questions").status_code == 401)

# --------------------------------------------------------------- 2. 2FA
print("\n2. 2FA integrity")
src_sec = (ROOT / "web" / "security.py").read_text()
m = re.search(r"valid_window\s*=\s*(\d+)", src_sec)
check("the TOTP window is small and explicit (<=1 step of drift)",
      m is not None and int(m.group(1)) <= 1, m.group(0) if m else "no valid_window set")
check("a code from far outside the window is refused",
      not sec.verify_totp(secret, pyotp.TOTP(secret).at(
          datetime.now(timezone.utc) + timedelta(minutes=10))))
check("an empty code is refused", not sec.verify_totp(secret, ""))
check("a code for a DIFFERENT secret is refused",
      not sec.verify_totp(secret, pyotp.TOTP(pyotp.random_base32()).now()))

stored = json.loads(sqlite3.connect(DB).execute(
    "SELECT recovery_codes FROM auth WHERE id=1").fetchone()[0])
check("recovery codes are bcrypt-hashed at rest",
      all(str(h).startswith("$2") for h in stored))
check("no recovery code appears in plaintext at rest",
      not any(code in json.dumps(stored) for code in CODES))
ok1, newjson = sec.consume_recovery_code(CODES[0], json.dumps(stored))
check("a recovery code verifies once", ok1)
ok2, _ = sec.consume_recovery_code(CODES[0], newjson)
check("...and is consumed, so a replay fails", not ok2)
check("consuming removes exactly one code", len(json.loads(newjson)) == len(stored) - 1)

# ------------------------------------------------------------ 3. secrets
print("\n3. secret handling")
c2 = client()
c2.post("/login", data={"username": "admin", "password": "correct-horse-battery"})
# Enrollment consumed its OTP. Authenticate this new session using a genuine
# unused recovery code rather than relying on replay within the same step.
c2.post("/2fa", data={"code": CODES[0]})
Q = {"id": "sec_q", "label": "Secret question",
     "concepts": [{"name": "a", "terms": ["alpha"], "mesh": []}],
     "api_keys": {"pubmed": SECRET_KEY_VALUE}}
c2.post("/api/questions", json=Q)

surfaces = {
    "GET /api/questions": c2.get("/api/questions").text,
    "GET /api/v1/questions": anon.get("/api/v1/questions",
        headers={"Authorization": "Bearer p7-token"}).text,
    "GET / (dashboard html)": c2.get("/").text,
    "GET /feed/sec_q.xml": c2.get("/feed/sec_q.xml").text,
    "GET /api/v1/meta": anon.get("/api/v1/meta",
        headers={"Authorization": "Bearer p7-token"}).text,
    "GET /api/source_health": c2.get("/api/source_health").text,
    "GET /api/signal_config/sec_q": c2.get("/api/signal_config/sec_q").text,
}
for name, body in surfaces.items():
    check(f"no plaintext API key in {name}", SECRET_KEY_VALUE not in body)
    check(f"no master key in {name}", KEY not in body)
    check(f"no SMTP password in {name}", "SMTP-PASSWORD-SHOULD-NEVER-APPEAR" not in body)

check("the dashboard reports only WHICH sources have a key",
      '"pubmed": true' in json.dumps(json.loads(c2.get("/api/questions").text)[0]
                                     .get("api_key_present", {})).replace("'", '"'))

# a traceback must not carry the ciphertext either
err = anon.get("/api/v1/records", headers={"Authorization": "Bearer p7-token"},
               params={"question_id": "no_such"})
check("a 404 body carries no key material",
      SECRET_KEY_VALUE not in err.text and KEY not in err.text)

print("\n3b. no code path prints a decrypted key")
offenders = []
for pyf in sorted((ROOT / "web").glob("*.py")):
    tree = ast.parse(pyf.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "print":
            seg = ast.get_source_segment(pyf.read_text(), node) or ""
            if re.search(r"decrypt|api_key|plain|EMAIL_PW|APP_SECRET|SMTP_PW|token", seg, re.I) \
               and "INGEST_TOKEN" not in seg:
                offenders.append(f"{pyf.name}: {seg[:90]}")
check("no print() in web/ renders a key, token or password", not offenders,
      "; ".join(offenders))
rsrc = "\n".join((ROOT / "R" / f).read_text() for f in os.listdir(ROOT / "R") if f.endswith(".R"))
bad_r = [l.strip() for l in rsrc.splitlines()
         if re.search(r"(message|cat|print|warning)\s*\(", l)
         and re.search(r"api_key|api_keys|decrypt|secret", l, re.I)]
check("no message()/cat() in R/ renders a key", not bad_r, "; ".join(bad_r[:3]))

print("\n3c. a WRONG master key degrades safely")
enc = json.loads(sqlite3.connect(DB).execute(
    "SELECT api_keys_enc FROM questions WHERE id='sec_q'").fetchone()[0])
res = subprocess.run([sys.executable, "-c",
    "import sys;from web import security as s;print(repr(s.decrypt_secret(sys.argv[1])))",
    enc["pubmed"]], cwd=ROOT, capture_output=True, text=True,
    env=dict(os.environ, APP_SECRET_KEY=Fernet.generate_key().decode()))
check("a wrong APP_SECRET_KEY returns empty, not an exception",
      res.returncode == 0 and res.stdout.strip() == "''",
      (res.stdout + res.stderr).strip()[-200:])
check("no ciphertext leaked into the output",
      enc["pubmed"][:20] not in (res.stdout + res.stderr))
res = subprocess.run([str(ROOT / "dev" / "tests" / "Rrun.sh"), "-e",
    'source("R/00_utils.R");source("R/07_db_questions.R");'
    'cat("[", .decrypt_key(commandArgs(TRUE)[1]), "]\\n", sep="")',
    "--args", enc["pubmed"]], cwd=ROOT, capture_output=True, text=True,
    env=dict(os.environ, APP_SECRET_KEY=Fernet.generate_key().decode()))
check("the R engine fails visibly instead of silently dropping a configured key",
      res.returncode != 0 and "Cannot decrypt configured API key" in res.stderr,
      (res.stdout + res.stderr).strip()[-200:])
check("the R decryption diagnostic contains no key material",
      all(value not in res.stdout + res.stderr for value in (enc["pubmed"], KEY, SECRET_KEY_VALUE)))

# ------------------------------------------------------- 4. cookies/transport
print("\n4. cookie flags")
c3 = client()
r = c3.post("/login", data={"username": "admin", "password": "correct-horse-battery"})
setc = r.headers.get("set-cookie", "")
check("session cookie is HttpOnly", "httponly" in setc.lower(), setc)
check("session cookie is Secure under SECURE_COOKIE=1", "secure" in setc.lower(), setc)
check("session cookie sets SameSite", "samesite" in setc.lower(), setc)
check("the session token is not guessable (>=32 bytes of entropy)",
      len(sec.new_session_token()) >= 40, str(len(sec.new_session_token())))
check("sessions expire", sec.is_expired(
    (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()))
check("an unparseable expiry is treated as expired (fail closed)",
      sec.is_expired("not-a-timestamp"))

# --------------------------------------------------------- 5. input handling
print("\n5. input handling")
py_src = {f.name: f.read_text() for f in (ROOT / "web").glob("*.py")}
sql_fstrings = []
for name, text in py_src.items():
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            parts = "".join(v.value for v in node.values if isinstance(v, ast.Constant))
            if re.search(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", parts, re.I):
                seg = ast.get_source_segment(text, node) or ""
                # interpolating a bound-parameter list or a constant is fine;
                # interpolating a VALUE is not.
                if not re.search(r"\{['\"]?,?['\"]?\s*\.join\(|marks|clause|where|threshold|table|col",
                                 seg):
                    sql_fstrings.append(f"{name}: {' '.join(seg.split())[:100]}")
check("no SQL f-string interpolates a request value", not sql_fstrings,
      "; ".join(sql_fstrings))

HOSTILE = ('</title></item></channel></rss><script>alert(1)</script>'
           ' & "quotes" \'single\' <![CDATA[x]]>')
con = sqlite3.connect(DB)
con.execute("INSERT INTO records(dedup_key,question_id,source,title,abstract,url,"
            "study_type,pub_date,score) VALUES(?,?,?,?,?,?,?,?,?)",
            ("hostile", "sec_q", "pubmed", HOSTILE, HOSTILE, "https://x/" + HOSTILE,
             "rct", "2026-08-01", 1.0))
con.execute("INSERT INTO signals(dedup_key,question_id,signal_type,source,title,"
            "summary,url,org,signal_date,confidence,score) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("hsig", "sec_q", "sec_filing", "test", HOSTILE, HOSTILE,
             "https://x/" + HOSTILE, HOSTILE, "2026-08-01", "authoritative", 1.0))
con.commit(); con.close()
for path in ("/feed/sec_q.xml", "/feed/sec_q/all.xml", "/feed/sec_q/rct.xml",
             "/signals/sec_q.xml", "/signals/sec_q/all.xml"):
    body = c2.get(path).text
    try:
        ET.fromstring(body)
        check(f"a hostile record keeps {path} well-formed", True)
    except ET.ParseError as e:
        check(f"a hostile record keeps {path} well-formed", False, str(e))
    check(f"no raw <script> in {path}", "<script>" not in body)
    check(f"the injected closing tags are escaped in {path}",
          "</rss><script>" not in body)

qid_attack = "x' OR '1'='1"
r = c2.get(f"/api/signal_config/{qid_attack}")
check("a quote-injection question id is a clean 404, not a SQL error",
      r.status_code == 404, str(r.status_code))
check("the record table survived the injection attempt",
      sqlite3.connect(DB).execute("SELECT COUNT(*) FROM records").fetchone()[0] > 0)

# ------------------------------------------------------- 6. password policy
print("\n6. password policy")
check("bcrypt is used for the admin password", sec.hash_pw("x" * 12).startswith("$2"))
check("bcrypt is used for recovery codes", all(h.startswith("$2") for h in stored))
check("bcrypt cost is at least 12",
      int(sec.hash_pw("x" * 12).split("$")[2]) >= 12, sec.hash_pw("x" * 12)[:10])
check("a minimum password length is enforced at setup",
      "len(password) < 10" in (ROOT / "web" / "app.py").read_text())
check("the password confirmation must match",
      "password != password2" in (ROOT / "web" / "app.py").read_text())
check("both bearer tokens are compared in constant time",
      (ROOT / "web" / "app.py").read_text().count("hmac.compare_digest") >= 2)

# ---------------------------------------------------- 7. dependency posture
print("\n7. dependency posture")
pins = {}
for df in ("Dockerfile", "Dockerfile.web", "Dockerfile.scheduler"):
    text = (ROOT / df).read_text()
    base = re.search(r"^FROM\s+(\S+)", text, re.M)
    pins[df] = {"base": base.group(1) if base else None,
                "floating_pip": re.findall(r'"([a-zA-Z0-9_.\[\]-]+)>=[^"]+"', text),
                "exact_pip": re.findall(r'"([a-zA-Z0-9_.\[\]-]+)==[^"]+"', text)}
for df, info in pins.items():
    check(f"{df} pins its base image to a version tag",
          info["base"] and ":" in info["base"] and not info["base"].endswith(":latest"),
          str(info["base"]))
floating = {df: i["floating_pip"] for df, i in pins.items() if i["floating_pip"]}
print(f"  [NOTE] pip requirements use floating lower bounds (>=), not exact pins: "
      f"{json.dumps(floating)}")
print(f"  [NOTE] R packages are installed by install2.r with no version pin; "
      f"reproducibility rests on the rocker base image's CRAN snapshot.")

print()
if fails:
    print(f"FAILED: {len(fails)} check(s):"); [print("   -", f) for f in fails]; sys.exit(1)
print("ALL PHASE 7 CHECKS PASSED"); sys.exit(0)
