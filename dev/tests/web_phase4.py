#!/usr/bin/env python3
"""Phase 4 — the FastAPI service, driven end to end against a scratch DB."""
import json, os, pathlib, sqlite3, sys
from xml.etree import ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
TMP = ROOT / "dev" / "state" / "tmp"; TMP.mkdir(parents=True, exist_ok=True)
DB = TMP / "p4.sqlite"
for suf in ("", "-wal", "-shm"):
    pathlib.Path(str(DB) + suf).unlink(missing_ok=True)

from cryptography.fernet import Fernet
KEY = Fernet.generate_key().decode()
os.environ.update(DB_PATH=str(DB), APP_SECRET_KEY=KEY, SECURE_COOKIE="0",
                  BASE_URL="http://testserver", API_TOKEN="test-api-token",
                  INGEST_TOKEN="test-ingest-token")

import pyotp
from fastapi.testclient import TestClient
from web.app import app
from web import security as sec

fails = []
def check(name, ok, detail=""):
    # detail is diagnostic: only useful when something failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if (detail and not ok) else ""))
    if not ok: fails.append(name)

c = TestClient(app, follow_redirects=False)
AUTH = {"Authorization": "Bearer test-api-token"}

# ---------------------------------------------------------------- setup/2FA
print("1. first run, setup, and mandatory 2FA")
r = c.get("/")
check("a fresh install redirects / to /setup",
      r.status_code in (302, 307) and "/setup" in r.headers.get("location", ""),
      f"{r.status_code} {r.headers.get('location')}")
check("GET /setup renders", c.get("/setup").status_code == 200)

r = c.post("/setup", data={"username": "admin", "password": "short", "password2": "short"})
_con = sqlite3.connect(DB); _n = _con.execute("SELECT COUNT(*) FROM auth").fetchone()[0]; _con.close()
check("setup rejects a too-short password and creates no admin",
      _n == 0 and "10 characters" in r.text, f"{r.status_code} admins={_n}")
r = c.post("/setup", data={"username": "admin", "password": "long-enough-here",
                           "password2": "different-password"})
_con = sqlite3.connect(DB); _n = _con.execute("SELECT COUNT(*) FROM auth").fetchone()[0]; _con.close()
check("setup rejects a mismatched confirmation", _n == 0, f"admins={_n}")
r = c.post("/setup", data={"username": "admin", "password": "correct-horse-battery", "password2": "correct-horse-battery"})
check("setup creates the admin", r.status_code in (200, 302, 303, 307), str(r.status_code))
r = c.post("/setup", data={"username": "intruder", "password": "another-long-password", "password2": "another-long-password"})
check("a second /setup is refused once an admin exists",
      r.status_code >= 400 or "/login" in r.headers.get("location", ""), str(r.status_code))
con = sqlite3.connect(DB)
row = con.execute("SELECT username,pw_hash FROM auth WHERE id=1").fetchone()
check("only one admin row exists and it is not 'intruder'", row[0] == "admin", str(row[0]))
check("the password is bcrypt-hashed, not stored", row[1].startswith("$2"), row[1][:8])
check("the plaintext password is nowhere in the auth row",
      "correct-horse-battery" not in json.dumps(list(row)))
con.close()

r = c.post("/login", data={"username": "admin", "password": "wrong-password"})
check("a wrong password is rejected", "Invalid credentials" in r.text, r.text[:120])
check("...and grants no session", c.get("/").status_code in (302, 303, 307)
      and "/login" in c.get("/").headers.get("location", ""))
r = c.post("/login", data={"username": "admin", "password": "correct-horse-battery"})
check("a correct password routes to the 2FA gate", "/2fa" in r.headers.get("location", ""),
      f"{r.status_code} {r.headers.get('location')}")

r = c.get("/")
check("the dashboard is unreachable before 2FA enrollment",
      r.status_code in (302, 303, 307) and "/2fa" in r.headers.get("location", ""),
      f"{r.status_code} {r.headers.get('location')}")
r = c.get("/api/questions")
check("the question API is unreachable before 2FA enrollment",
      r.status_code in (302, 303, 307, 401, 403), str(r.status_code))

r = c.get("/2fa/enroll")
check("enrollment shows a QR/secret", r.status_code == 200)
con = sqlite3.connect(DB)
secret = con.execute("SELECT totp_secret FROM auth WHERE id=1").fetchone()[0]
con.close()
check("a TOTP secret was provisioned", bool(secret))

r = c.post("/2fa/enroll", data={"code": "000000"})
_con = sqlite3.connect(DB)
_en = _con.execute("SELECT totp_enabled FROM auth WHERE id=1").fetchone()[0]; _con.close()
check("enrollment rejects a wrong TOTP code and stays un-enrolled",
      _en == 0 and "didn" in r.text, f"totp_enabled={_en}")
check("...and the dashboard is still closed",
      c.get("/").status_code in (302, 303, 307))
totp = pyotp.TOTP(secret)
r = c.post("/2fa/enroll", data={"code": totp.now()})
check("enrollment accepts a valid TOTP code", r.status_code in (200, 302, 303, 307), str(r.status_code))
codes = [w.strip() for w in __import__("re").findall(r"\b(?:[0-9a-f]{4}-){3}[0-9a-f]{4}\b", r.text)]
check("recovery codes are shown exactly once at enrollment", len(codes) >= 8, f"{len(codes)} codes")
con = sqlite3.connect(DB)
stored = json.loads(con.execute("SELECT recovery_codes FROM auth WHERE id=1").fetchone()[0])
con.close()
check("recovery codes are bcrypt-hashed at rest",
      all(str(x).startswith("$2") for x in stored), str(stored[:1]))
check("no recovery code is stored in plaintext",
      not any(code in json.dumps(stored) for code in codes))

r = c.get("/")
check("the dashboard is reachable once enrolled", r.status_code == 200, str(r.status_code))

# ----------------------------------------------------------- question + keys
print("\n2. question API and the API-key invariants")
Q = {"id": "glp1_mash", "label": "GLP-1 in MASH",
     "concepts": [{"name": "drug", "terms": ["semaglutide", "glp-1"], "mesh": ["Glucagon-Like Peptide 1"]},
                  {"name": "disease", "terms": ["mash", "steatohepatitis"], "mesh": []}],
     "study_filter": ["randomized controlled trial"], "from_days_back": 14,
     "enabled": True, "sources": {"pubmed": True, "biorxiv": False},
     "api_keys": {"pubmed": "PLAINTEXT-NCBI-KEY"},
     "delivery": ["file", "zotero"], "email_enabled": True,
     "email_cron": "0 7 * * 1", "email_to": ["a@example.com", "b@example.com"],
     "digest_mode": "threshold", "min_score": 1.25}
r = c.post("/api/questions", json=Q)
check("POST /api/questions succeeds when authenticated", r.status_code == 200, r.text[:200])

got = c.get("/api/questions").json()[0]
check("the plaintext key never appears in the GET response",
      "PLAINTEXT-NCBI-KEY" not in json.dumps(got))
check("only a per-source 'present' boolean is returned",
      got["api_key_present"] == {"pubmed": True}, json.dumps(got.get("api_key_present")))
con = sqlite3.connect(DB)
enc = json.loads(con.execute("SELECT api_keys_enc FROM questions WHERE id=?", (Q["id"],)).fetchone()[0])
con.close()
check("the key is Fernet ciphertext at rest", enc["pubmed"].startswith("gAAAA"), enc["pubmed"][:8])
check("the plaintext key is not in the DB column", "PLAINTEXT-NCBI-KEY" not in json.dumps(enc))
check("the ciphertext decrypts back to the original (the path R uses)",
      sec.decrypt_secret(enc["pubmed"]) == "PLAINTEXT-NCBI-KEY")

Q2 = dict(Q, label="GLP-1 in MASH (v2)", api_keys={})
c.post("/api/questions", json=Q2)
con = sqlite3.connect(DB)
enc2 = json.loads(con.execute("SELECT api_keys_enc FROM questions WHERE id=?", (Q["id"],)).fetchone()[0])
con.close()
check("omitting the key on update preserves the stored one",
      sec.decrypt_secret(enc2.get("pubmed", "")) == "PLAINTEXT-NCBI-KEY")

c.post("/api/questions", json=dict(Q, api_keys={"pubmed": "__CLEAR__"}))
con = sqlite3.connect(DB)
enc3 = json.loads(con.execute("SELECT api_keys_enc FROM questions WHERE id=?", (Q["id"],)).fetchone()[0])
con.close()
check("__CLEAR__ removes the stored key", "pubmed" not in enc3, json.dumps(enc3))
c.post("/api/questions", json=Q)  # restore

print("\n3. every per-question field round-trips")
got = c.get("/api/questions").json()[0]
for field in ("id", "label", "study_filter", "from_days_back", "enabled",
              "sources", "delivery", "email_enabled", "email_cron", "email_to",
              "digest_mode", "min_score"):
    check(f"round-trip: {field}", got[field] == Q[field], f"{got[field]!r} != {Q[field]!r}")
check("round-trip: concepts (names, terms, mesh, order)",
      got["concepts"] == Q["concepts"], json.dumps(got["concepts"]))

# ------------------------------------------------------------------- feeds
print("\n4. feeds")
con = sqlite3.connect(DB)
RECS = [
    ("doi:1", "pubmed", "10.1/1", "An RCT of semaglutide", "rct", 0, 5.0),
    ("doi:2", "europepmc", "10.1/2", "A systematic review & meta-analysis", "systematic_review", 0, 4.0),
    ("doi:3", "biorxiv", "10.1/3", "Preprint: <script>alert(1)</script> & \"quotes\"", "rct", 1, 3.0),
    ("doi:4", "crossref", "10.1/4", "Low-score note", "other", 0, 0.1),
]
for k, src, doi, title, st, pp, score in RECS:
    con.execute("INSERT INTO records(dedup_key,question_id,source,doi,title,study_type,"
                "is_preprint,score,pub_date,url,abstract) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (k, "glp1_mash", src, doi, title, st, pp, score, "2026-08-01",
                 "https://doi.org/" + doi, "abstract with <b>tags</b> & ampersands"))
con.commit(); con.close()

def feed_ok(path, expect_titles=None, expect_status=200):
    r = c.get(path)
    if r.status_code != expect_status:
        check(f"GET {path} -> {expect_status}", False, str(r.status_code)); return None
    check(f"GET {path} -> {expect_status}", True)
    if expect_status != 200: return None
    try:
        root = ET.fromstring(r.text)
    except ET.ParseError as e:
        check(f"{path} body is well-formed XML", False, str(e)); return None
    check(f"{path} body is well-formed XML", True)
    titles = [t.text for t in root.iter("title")][1:]
    if expect_titles is not None:
        check(f"{path} returns the expected items", len(titles) == expect_titles,
              f"{len(titles)} items: {titles}")
    return root

feed_ok("/feed/glp1_mash/all.xml", 4)
feed_ok("/feed/glp1_mash.xml", 3)          # digest_mode=threshold, min_score=1.25
feed_ok("/feed/glp1_mash/rct.xml", 2)
feed_ok("/feed/glp1_mash/systematic_review.xml", 1)
root = feed_ok("/feed/glp1_mash/preprints.xml", 1)
if root is not None:
    cats = [e.text for e in root.iter("category")]
    check("the preprint feed marks items with a 'preprint' category",
          any("preprint" in (x or "").lower() for x in cats), str(cats))
    check("study-type and source categories are present",
          any("Randomized" in (x or "") for x in cats) and any("biorxiv" in (x or "") for x in cats),
          str(cats))
feed_ok("/feed/glp1_mash/not_a_type.xml", expect_status=404)
feed_ok("/feed/no_such_question.xml", expect_status=404)

r = c.get("/feed/glp1_mash/all.xml")
check("hostile characters in a title do not break the XML",
      "<script>" not in r.text and "&lt;script&gt;" in r.text)
check("raw ampersands are escaped", " & " not in r.text.replace("&amp;", ""))

# ------------------------------------------------------------ session gating
print("\n5. session gating")
c.post("/logout")
r = c.get("/")
check("after logout the dashboard is blocked again",
      r.status_code in (302, 303, 307) or r.status_code >= 400, str(r.status_code))
r = c.get("/api/questions")
check("after logout the question API is blocked",
      r.status_code in (302, 303, 307, 401, 403), str(r.status_code))
fresh = TestClient(app, follow_redirects=False)
check("a client with no session cannot read /api/questions",
      fresh.get("/api/questions").status_code in (302, 303, 307, 401, 403))
check("a client with no session cannot write /api/questions",
      fresh.post("/api/questions", json=Q).status_code in (302, 303, 307, 401, 403))
check("a forged session cookie is rejected",
      TestClient(app, follow_redirects=False, cookies={"rf_session": "forged"})
      .get("/").status_code in (302, 303, 307, 401, 403))
check("feeds stay open with no session at all", fresh.get("/feed/glp1_mash.xml").status_code == 200)
check("/healthz stays open", fresh.get("/healthz").status_code == 200)

# ---------------------------------------------------------- recovery codes
print("\n6. recovery codes are single-use")
c2 = TestClient(app, follow_redirects=False)
c2.post("/login", data={"username": "admin", "password": "correct-horse-battery"})
r = c2.post("/2fa", data={"code": codes[0]})
check("a recovery code passes 2FA", r.status_code in (200, 302, 303, 307), str(r.status_code))
check("...and the dashboard opens", c2.get("/").status_code == 200)
c3 = TestClient(app, follow_redirects=False)
c3.post("/login", data={"username": "admin", "password": "correct-horse-battery"})
r = c3.post("/2fa", data={"code": codes[0]})
check("the SAME recovery code is refused on reuse",
      r.status_code >= 400 or "2fa" in r.headers.get("location", "").lower()
      or c3.get("/").status_code != 200, str(r.status_code))
c4 = TestClient(app, follow_redirects=False)
c4.post("/login", data={"username": "admin", "password": "correct-horse-battery"})
r = c4.post("/2fa", data={"code": "111111"})
check("a wrong TOTP code is refused",
      r.status_code >= 400 or c4.get("/").status_code != 200, str(r.status_code))

# ------------------------------------------------ /api/v1 (researchR client)
print("\n7. /api/v1 — the token surface the researchR client uses")
anon = TestClient(app, follow_redirects=False)
check("no token -> 401", anon.get("/api/v1/questions").status_code == 401)
check("wrong token -> 401",
      anon.get("/api/v1/questions", headers={"Authorization": "Bearer nope"}).status_code == 401)
check("a dashboard session is NOT accepted as an API token",
      c2.get("/api/v1/questions").status_code == 401)
check("valid token -> 200", anon.get("/api/v1/questions", headers=AUTH).status_code == 200)

meta = anon.get("/api/v1/meta", headers=AUTH).json()
check("/meta publishes the study-type vocabulary",
      meta["study_types"][0] == "systematic_review" and meta["study_types"][-1] == "unclassified",
      json.dumps(meta["study_types"]))
check("/meta publishes signal types and confidence levels",
      "sec_filing" in meta["signal_types"] and meta["confidence_levels"] == ["unverified", "reported", "authoritative"])

qs = anon.get("/api/v1/questions", headers=AUTH).json()
check("/questions returns the seeded question", qs[0]["id"] == "glp1_mash")
check("/questions still never returns a plaintext key",
      "PLAINTEXT-NCBI-KEY" not in json.dumps(qs))

rec = anon.get("/api/v1/records", headers=AUTH, params={"question_id": "glp1_mash"}).json()
check("/records returns every stored record", rec["total"] == 4, str(rec["total"]))
check("/records rows carry the full column set",
      set(rec["records"][0]) == set(["dedup_key", "question_id", "source", "ext_id", "doi",
          "title", "abstract", "authors", "journal", "pub_date", "url", "oa_url", "provenance", "type",
          "study_type", "is_preprint", "score", "first_seen", "emailed"]),
      json.dumps(sorted(rec["records"][0])))
r = anon.get("/api/v1/records", headers=AUTH, params={"question_id": "glp1_mash", "study_type": "rct"}).json()
check("/records filters by study_type", r["total"] == 2, str(r["total"]))
r = anon.get("/api/v1/records", headers=AUTH, params={"question_id": "glp1_mash", "is_preprint": True}).json()
check("/records filters by is_preprint", r["total"] == 1, str(r["total"]))
r = anon.get("/api/v1/records", headers=AUTH, params={"question_id": "glp1_mash", "min_score": 3.5}).json()
check("/records filters by min_score", r["total"] == 2, str(r["total"]))
r = anon.get("/api/v1/records", headers=AUTH, params={"question_id": "glp1_mash", "apply_threshold": True}).json()
check("/records can apply the question's digest threshold", r["total"] == 3, str(r["total"]))
r = anon.get("/api/v1/records", headers=AUTH, params={"question_id": "glp1_mash", "limit": 2}).json()
check("/records paginates", len(r["records"]) == 2 and r["total"] == 4, json.dumps([r["total"], len(r["records"])]))
r2 = anon.get("/api/v1/records", headers=AUTH, params={"question_id": "glp1_mash", "limit": 2, "offset": 2}).json()
check("/records offset returns a disjoint page",
      not ({x["dedup_key"] for x in r["records"]} & {x["dedup_key"] for x in r2["records"]}))
r = anon.get("/api/v1/records", headers=AUTH, params={"question_id": "glp1_mash", "limit": 999999}).json()
check("/records clamps an oversized limit", r["limit"] == meta["max_page"], str(r["limit"]))
check("/records 404s on an unknown question",
      anon.get("/api/v1/records", headers=AUTH, params={"question_id": "nope"}).status_code == 404)
check("/records 400s on an unknown study_type",
      anon.get("/api/v1/records", headers=AUTH,
               params={"question_id": "glp1_mash", "study_type": "bogus"}).status_code == 400)

con = sqlite3.connect(DB)
for k, st, conf, score in [("s1", "sec_filing", "authoritative", 9.0),
                           ("s2", "press_release", "unverified", 2.0),
                           ("s3", "trial_result", "reported", 5.0)]:
    con.execute("INSERT INTO signals(dedup_key,question_id,signal_type,source,title,"
                "signal_date,confidence,score) VALUES(?,?,?,?,?,?,?,?)",
                (k, "glp1_mash", st, "test", "S " + k, "2026-08-10", conf, score))
con.commit(); con.close()
sg = anon.get("/api/v1/signals", headers=AUTH, params={"question_id": "glp1_mash"}).json()
check("/signals returns every signal", sg["total"] == 3, str(sg["total"]))
sg = anon.get("/api/v1/signals", headers=AUTH,
              params={"question_id": "glp1_mash", "min_confidence": "reported"}).json()
check("/signals honours min_confidence as a floor, not equality", sg["total"] == 2, str(sg["total"]))
sg = anon.get("/api/v1/signals", headers=AUTH,
              params={"question_id": "glp1_mash", "signal_type": "sec_filing"}).json()
check("/signals filters by signal_type", sg["total"] == 1, str(sg["total"]))
check("/signals 400s on an unknown confidence level",
      anon.get("/api/v1/signals", headers=AUTH,
               params={"question_id": "glp1_mash", "min_confidence": "gossip"}).status_code == 400)

check("/source_health is reachable with a token",
      anon.get("/api/v1/source_health", headers=AUTH).status_code == 200)
check("/signal_config is reachable with a token",
      anon.get("/api/v1/signal_config/glp1_mash", headers=AUTH).status_code == 200)

r = anon.post("/api/v1/questions", headers=AUTH,
              json=dict(Q, id="via_api", label="Created by the R client"))
check("the client can create a question", r.status_code == 200, r.text[:150])
check("...and it comes back on the next read",
      any(x["id"] == "via_api" for x in anon.get("/api/v1/questions", headers=AUTH).json()))
check("the client can delete a question",
      anon.delete("/api/v1/questions/via_api", headers=AUTH).status_code == 200)
check("...and it is gone",
      not any(x["id"] == "via_api" for x in anon.get("/api/v1/questions", headers=AUTH).json()))

print()
if fails:
    print(f"FAILED: {len(fails)} check(s):"); [print("   -", f) for f in fails]; sys.exit(1)
print("ALL PHASE 4 CHECKS PASSED"); sys.exit(0)
