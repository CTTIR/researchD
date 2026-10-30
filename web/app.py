# ============================================================
# app.py — FastAPI web service.
#
#   PUBLIC (no auth):
#     GET  /feed/{id}.xml                per-question RSS (Zotero subscribes)
#     GET  /feed/{id}/{stype}.xml        study-type filtered
#     GET  /feed/{id}/preprints.xml      preprint-only
#     GET  /healthz
#
#   AUTH FLOW (session cookie + TOTP 2FA):
#     GET/POST /setup                    first-run: create admin (once)
#     GET/POST /login                    password -> then 2FA gate
#     GET/POST /2fa                      TOTP (or recovery code)
#     GET/POST /2fa/enroll               show QR, confirm code, store secret
#     POST     /logout
#
#   PROTECTED (session + mfa_passed):
#     GET   /                            dashboard (landing + admin)
#     GET   /api/questions               list (keys never returned)
#     POST  /api/questions               create / update
#     DELETE /api/questions/{id}
#
# Reads/writes the same SQLite the R engine uses. WAL + busy_timeout
# keep the two services from colliding.
# ============================================================
import os, json, sqlite3, secrets as pysecrets
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape, quoteattr
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, Request, Response, Form, Cookie
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictBool, StrictStr, field_validator, model_validator
from typing import Literal
from urllib.parse import urlsplit
from croniter import croniter
import jinja2

from web import security as sec

DB_PATH = os.environ.get("DB_PATH", "/app/db/pipeline.sqlite")
BASE_URL = os.environ.get("BASE_URL", "")
FEED_LIMIT = int(os.environ.get("FEED_LIMIT", "100"))
COOKIE = "rf_session"
SECURE_COOKIE = os.environ.get("SECURE_COOKIE", "1") == "1"  # set 0 if no HTTPS yet
# Machine-to-machine token for the /api/ingest endpoint (n8n etc.).
# Set INGEST_TOKEN in the environment; if unset, the ingest endpoint is
# disabled (returns 503) so it can never accept unauthenticated writes.
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "")
# Token for the machine-readable /api/v1 surface that the researchR R client
# speaks to. The dashboard's session + TOTP flow is a browser flow; a package
# cannot carry it, so /api/v1 is bearer-token authenticated instead. Unset =
# the whole surface returns 503, exactly like /api/ingest.
API_TOKEN = os.environ.get("API_TOKEN", "")

BASE_DIR = Path(__file__).parent
app = FastAPI(title="Research Feed Service")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

@app.middleware("http")
async def response_security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path == "/" or request.url.path.startswith(("/api/", "/setup", "/login", "/2fa")):
        response.headers["Cache-Control"] = "no-store"
    return response


_jenv = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(BASE_DIR / "templates")),
    autoescape=jinja2.select_autoescape(["html", "xml"]),
    cache_size=0,
)
def render(name, **ctx) -> HTMLResponse:
    return HTMLResponse(_jenv.get_template(name).render(**ctx))


# ---- db -------------------------------------------------------
def db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=10000;")
    con.execute("PRAGMA foreign_keys=ON;")
    return con

def init_schema():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    con = db()
    con.executescript((BASE_DIR.parent / "db" / "schema.sql").read_text())
    _migrate(con)
    con.commit(); con.close()

# Additive migrations: schema.sql only CREATEs tables IF NOT EXISTS, so a
# pre-existing DB won't gain new columns from it. Add them here, guarded.
# Each entry: (table, column, coldef). Safe to run on every startup.
_MIGRATIONS = [
    ("questions", "digest_mode", "TEXT DEFAULT 'all'"),
    ("questions", "min_score",   "REAL DEFAULT 0"),
    ("signal_config", "feed_categories", "TEXT DEFAULT '{}'"),
    ("signal_config", "instant_alerts", "INTEGER DEFAULT 0"),
    ("records", "oa_url", "TEXT"),
    ("records", "provenance", "TEXT DEFAULT '[]'"),
]
def _migrate(con):
    for table, col, coldef in _MIGRATIONS:
        cols = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coldef}")

# Initialize at import time so tables exist before the first request, even
# under TestClient (whose startup events only fire inside a context manager)
# or before the R engine has ever run.
try:
    init_schema()
except Exception as _e:
    # if the schema file isn't reachable at import (e.g. unusual CWD), the
    # startup hook below is the fallback.
    pass

# `@app.on_event` is deprecated and slated for removal; the lifespan context
# manager is the supported hook. Kept as a fallback for the import-time call
# above, which is what actually covers TestClient and cold starts.
@asynccontextmanager
async def _lifespan(_app):
    init_schema()
    yield

app.router.lifespan_context = _lifespan


# ---- auth plumbing -------------------------------------------
SOURCES = ["pubmed", "europepmc", "openalex", "semanticscholar", "crossref",
           "clinicaltrials", "biorxiv"]
SOURCES_WITH_KEYS = ["pubmed", "openalex", "semanticscholar"]  # which accept a personal key

def get_admin(con):
    return con.execute("SELECT * FROM auth WHERE id=1").fetchone()

def current_session(con, token):
    if not token:
        return None
    s = con.execute("SELECT * FROM sessions WHERE token=?", (token,)).fetchone()
    if not s or sec.is_expired(s["expires_at"]):
        return None
    return s

def require_user(request: Request):
    """Full gate: valid session AND mfa passed. Returns username or raises redirect."""
    token = request.cookies.get(COOKIE)
    con = db()
    try:
        admin = get_admin(con)
        if not admin:
            raise HTTPException(307, headers={"Location": "/setup"})
        s = current_session(con, token)
        if not s:
            raise HTTPException(307, headers={"Location": "/login"})
        # 2FA is mandatory: any session that hasn't passed MFA is sent to /2fa
        # (which forwards to enrollment if TOTP isn't set up yet).
        if not s["mfa_passed"]:
            raise HTTPException(307, headers={"Location": "/2fa"})
        return admin["username"]
    finally:
        con.close()

def set_session_cookie(resp, token):
    resp.set_cookie(COOKIE, token, httponly=True, secure=SECURE_COOKIE,
                    samesite="lax", max_age=12 * 3600)


# ---------- study-type vocab (mirror of R/06_classify.R) -------
STUDY_LABELS = {
    "systematic_review": "Systematic review / meta-analysis",
    "rct": "Randomized controlled trial",
    "clinical_trial": "Clinical trial",
    "guideline": "Guideline / consensus",
    "observational": "Observational / comparative",
    "case_report": "Case report",
    "review": "Review (narrative)",
    "other": "Editorial / letter / news",
    "unclassified": "Unclassified / not yet indexed",
}


# ================= PUBLIC: RSS FEEDS ==========================
def _rss_item(r) -> str:
    title = escape(r["title"] or "(untitled)")
    link = escape(r["url"] or "")
    guid = escape(r["dedup_key"])
    bits = []
    if r["authors"]: bits.append(escape(r["authors"][:300]))
    if r["journal"]: bits.append(escape(r["journal"]))
    if r["abstract"]: bits.append(escape(r["abstract"][:1000]))
    # free open-access full text, if Unpaywall found one
    try:
        if r["oa_url"]:
            bits.append(f'Free full text: {escape(r["oa_url"])}')
    except (KeyError, IndexError):
        pass
    description = " — ".join(bits)
    cats = ""
    if r["study_type"]:
        cats += f"<category>{escape(STUDY_LABELS.get(r['study_type'], r['study_type']))}</category>"
    if r["source"]:
        cats += f"<category>{escape(r['source'])}</category>"
    try:
        if r["is_preprint"]:
            cats += "<category>preprint</category>"
    except (KeyError, IndexError):
        pass
    try:
        dt = datetime.fromisoformat(r["first_seen"]).replace(tzinfo=timezone.utc)
    except Exception:
        dt = datetime.now(timezone.utc)
    return (f"<item><title>{title}</title><link>{link}</link>"
            f"<guid isPermaLink='false'>{guid}</guid>"
            f"<description>{description}</description>{cats}"
            f"<pubDate>{format_datetime(dt)}</pubDate></item>")

def _render_feed(qrow, rows, self_url) -> str:
    title = escape(f"Research feed — {qrow['label']}")
    items = "\n".join(_rss_item(r) for r in rows)
    now = format_datetime(datetime.now(timezone.utc))
    return (f"<?xml version='1.0' encoding='UTF-8'?>"
            f"<rss version='2.0' xmlns:atom='http://www.w3.org/2005/Atom'>"
            f"<channel><title>{title}</title><link>{escape(self_url)}</link>"
            f"<atom:link href={quoteattr(self_url)} rel='self' type='application/rss+xml'/>"
            f"<description>Auto-curated literature feed</description>"
            f"<lastBuildDate>{now}</lastBuildDate>{items}</channel></rss>")

def _feed_response(qid, request, where="", params=(), apply_threshold=False):
    con = db()
    qrow = con.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
    if not qrow:
        con.close(); raise HTTPException(404, "No such question")
    # Recall safety-valve: the default + study-type feeds honor the question's
    # digest threshold (the high-signal stream). all.xml passes apply_threshold
    # =False to show everything that was stored (nothing dropped from the DB).
    extra = ""
    if apply_threshold:
        qd = dict(qrow)
        if (qd.get("digest_mode") or "all") == "threshold":
            extra = f" AND score >= {float(qd.get('min_score') or 0)}"
    sql = "SELECT * FROM records WHERE question_id=? " + where + extra + \
          " ORDER BY first_seen DESC LIMIT ?"
    rows = con.execute(sql, (qid, *params, FEED_LIMIT)).fetchall()
    con.close()
    self_url = (BASE_URL or str(request.base_url).rstrip("/")) + request.url.path
    return Response(_render_feed(qrow, rows, self_url), media_type="application/rss+xml")

@app.get("/feed/{qid}.xml")
def feed(qid: str, request: Request):
    # default feed = the curated/high-signal stream (threshold applied)
    return _feed_response(qid, request, apply_threshold=True)

@app.get("/feed/{qid}/all.xml")
def feed_all(qid: str, request: Request):
    # the recall safety valve: everything stored, unfiltered
    return _feed_response(qid, request, apply_threshold=False)

@app.get("/feed/{qid}/preprints.xml")
def feed_preprints(qid: str, request: Request):
    return _feed_response(qid, request, "AND is_preprint=1")

@app.get("/feed/{qid}/{stype}.xml")
def feed_by_type(qid: str, stype: str, request: Request):
    if stype not in STUDY_LABELS:
        raise HTTPException(404, "Unknown study type")
    return _feed_response(qid, request, "AND study_type=?", (stype,), apply_threshold=True)


# ================= SIGNALS FEEDS (separate stream) ============
SIGNAL_TYPES = ["grant","trial_registration","trial_result","regulatory",
                "sec_filing","patent","press_release","conference",
                "literature_alert","institution","aggregator","trade_press","news",
                "ir_alert"]
CONFIDENCE_RANK = {"unverified": 0, "reported": 1, "authoritative": 2}

def _signal_item(r) -> str:
    title = escape(r["title"] or "(untitled)")
    link = escape(r["url"] or "")
    guid = escape(r["dedup_key"])
    bits = []
    if r["org"]: bits.append(escape(r["org"]))
    if r["summary"]: bits.append(escape(r["summary"][:1000]))
    description = " — ".join(bits)
    cats = f"<category>{escape(r['signal_type'])}</category>"
    cats += f"<category>{escape(r['source'])}</category>"
    cats += f"<category>confidence:{escape(r['confidence'] or 'reported')}</category>"
    try:
        dt = datetime.fromisoformat((r["first_seen"] or "").replace(" ", "T")).replace(tzinfo=timezone.utc)
    except Exception:
        dt = datetime.now(timezone.utc)
    return (f"<item><title>{title}</title><link>{link}</link>"
            f"<guid isPermaLink='false'>{guid}</guid>"
            f"<description>{description}</description>{cats}"
            f"<pubDate>{format_datetime(dt)}</pubDate></item>")

def _signals_feed_response(qid, request, where="", params=()):
    con = db()
    qrow = con.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
    if not qrow:
        con.close(); raise HTTPException(404, "No such question")
    # honor the signal min_confidence threshold for the default signals feed
    cfg = con.execute("SELECT min_confidence FROM signal_config WHERE question_id=?",
                      (qid,)).fetchone()
    conf_sql, conf_params = "", []
    if cfg and where == "":  # only the default feed applies the confidence gate
        minc = cfg["min_confidence"] or "unverified"
        allowed = [k for k, v in CONFIDENCE_RANK.items() if v >= CONFIDENCE_RANK.get(minc, 0)]
        conf_sql = " AND confidence IN (%s)" % ",".join("?" * len(allowed))
        conf_params = allowed
    sql = ("SELECT * FROM signals WHERE question_id=? " + where + conf_sql +
           " ORDER BY first_seen DESC LIMIT ?")
    rows = con.execute(sql, (qid, *params, *conf_params, FEED_LIMIT)).fetchall()
    con.close()
    self_url = (BASE_URL or str(request.base_url).rstrip("/")) + request.url.path
    title = escape(f"Signals — {qrow['label']}")
    items = "\n".join(_signal_item(r) for r in rows)
    now = format_datetime(datetime.now(timezone.utc))
    xml = (f"<?xml version='1.0' encoding='UTF-8'?>"
           f"<rss version='2.0' xmlns:atom='http://www.w3.org/2005/Atom'>"
           f"<channel><title>{title}</title><link>{escape(self_url)}</link>"
           f"<atom:link href={quoteattr(self_url)} rel='self' type='application/rss+xml'/>"
           f"<description>Early intelligence: trials, grants, filings, patents, press</description>"
           f"<lastBuildDate>{now}</lastBuildDate>{items}</channel></rss>")
    return Response(xml, media_type="application/rss+xml")

@app.get("/signals/{qid}.xml")
def signals_feed(qid: str, request: Request):
    return _signals_feed_response(qid, request)

@app.get("/signals/{qid}/all.xml")
def signals_feed_all(qid: str, request: Request):
    # unfiltered: every confidence tier including unverified scraped leads
    return _signals_feed_response(qid, request, where="AND 1=1")

@app.get("/signals/{qid}/{stype}.xml")
def signals_feed_by_type(qid: str, stype: str, request: Request):
    if stype not in SIGNAL_TYPES:
        raise HTTPException(404, "Unknown signal type")
    return _signals_feed_response(qid, request, "AND signal_type=?", (stype,))




# ============================================================
# SIGNAL CONFIG API — the web-UI frontend for the signals track.
# Mirrors the question API patterns: keys are write-only (the UI
# only ever learns WHICH sources have a stored key), encrypted at
# rest with the same Fernet master key.
# ============================================================
SIGNAL_SOURCES = ["sec_edgar", "nih_reporter", "openfda", "trials",
                  "patentsview", "epo_ops", "feeds", "cortellis", "citeline"]
SIGNAL_KEY_SOURCES = ["epo_ops", "openfda", "patentsview", "cortellis", "citeline"]
FEED_CATEGORIES = ["literature_alert", "institution", "aggregator",
                   "trade_press", "news", "ir_alert", "conference"]

def valid_http_url(value):
    try:
        parsed = urlsplit(value)
        return (parsed.scheme.lower() in ("http", "https") and bool(parsed.hostname)
                and not parsed.username and not parsed.password
                and not any(ord(c) < 32 for c in value))
    except (TypeError, ValueError):
        return False


class SignalConfig(BaseModel):
    sources: dict[str, StrictBool] = Field(default_factory=dict)
    min_confidence: Literal["unverified", "reported", "authoritative"] = "unverified"
    instant_alerts: StrictBool = False
    feed_categories: dict[str, StrictBool] = Field(default_factory=dict)
    scrape_targets: list[dict] = Field(default_factory=list)
    api_keys: dict[str, StrictStr] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_config(self):
        for values, known, label in ((self.sources, SIGNAL_SOURCES, "signal source"),
                                     (self.feed_categories, FEED_CATEGORIES, "feed category"),
                                     (self.api_keys, SIGNAL_KEY_SOURCES, "signal key")):
            unknown = set(values) - set(known)
            if unknown:
                raise ValueError(f"Unknown {label}: {', '.join(sorted(unknown))}")
        for target in self.scrape_targets:
            url = target.get("url", "")
            if not isinstance(url, str) or not valid_http_url(url):
                raise ValueError("Scrape target URL must be an absolute HTTP(S) URL without credentials")
            if target.get("type") not in ("rss", "html"):
                raise ValueError("Scrape target type must be rss or html")
            for field in ("name", "selector"):
                if field in target and not isinstance(target[field], str):
                    raise ValueError(f"Scrape target {field} must be text")
            target.setdefault("name", "")
        return self

@app.get("/api/signal_config/{qid}")
def get_signal_config(qid: str, user: str = Depends(require_user)):
    con = db()
    try:
        if not con.execute("SELECT 1 FROM questions WHERE id=?", (qid,)).fetchone():
            raise HTTPException(404, "No such question")
        row = con.execute("SELECT * FROM signal_config WHERE question_id=?",
                          (qid,)).fetchone()
        if not row:
            return {"sources": {}, "min_confidence": "unverified",
                    "instant_alerts": False, "feed_categories": {},
                    "scrape_targets": [], "api_key_present": {}}
        try: keymap = json.loads(row["api_keys_enc"] or "{}")
        except json.JSONDecodeError: keymap = {}
        return {
            "sources": json.loads(row["sources"] or "{}"),
            "min_confidence": row["min_confidence"] or "unverified",
            "instant_alerts": bool(row["instant_alerts"]
                                   if "instant_alerts" in row.keys() else 0),
            "feed_categories": json.loads(row["feed_categories"] or "{}")
                               if "feed_categories" in row.keys() else {},
            "scrape_targets": json.loads(row["scrape_targets"] or "[]"),
            "api_key_present": {k: bool(v) for k, v in keymap.items()},
        }
    finally:
        con.close()

@app.post("/api/signal_config/{qid}")
def upsert_signal_config(qid: str, cfg: SignalConfig,
                         user: str = Depends(require_user)):
    if cfg.min_confidence not in ("unverified", "reported", "authoritative"):
        raise HTTPException(422, "invalid min_confidence")
    for t in cfg.scrape_targets:
        if not isinstance(t, dict) or not t.get("url") or t.get("type") not in ("rss", "html"):
            raise HTTPException(422, "scrape_targets entries need url and type rss|html")
    con = db()
    try:
        if not con.execute("SELECT 1 FROM questions WHERE id=?", (qid,)).fetchone():
            raise HTTPException(404, "No such question")
        # merge keys like the question flow: keep existing, overwrite provided,
        # "__CLEAR__" removes
        existing = con.execute("SELECT api_keys_enc FROM signal_config WHERE question_id=?",
                               (qid,)).fetchone()
        keymap = {}
        if existing and existing["api_keys_enc"]:
            try: keymap = json.loads(existing["api_keys_enc"])
            except json.JSONDecodeError: keymap = {}
        for src, plain in (cfg.api_keys or {}).items():
            if src not in SIGNAL_KEY_SOURCES: continue
            if plain == "__CLEAR__": keymap.pop(src, None)
            elif plain: keymap[src] = sec.encrypt_secret(plain)
        con.execute("""
            INSERT INTO signal_config(question_id, sources, min_confidence,
              instant_alerts, feed_categories, scrape_targets, api_keys_enc)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(question_id) DO UPDATE SET
              sources=excluded.sources, min_confidence=excluded.min_confidence,
              instant_alerts=excluded.instant_alerts,
              feed_categories=excluded.feed_categories,
              scrape_targets=excluded.scrape_targets,
              api_keys_enc=excluded.api_keys_enc
        """, (qid, json.dumps(cfg.sources), cfg.min_confidence,
              int(cfg.instant_alerts), json.dumps(cfg.feed_categories),
              json.dumps(cfg.scrape_targets), json.dumps(keymap)))
        con.commit()
        return {"ok": True}
    finally:
        con.close()

# ============================================================
# SOURCE HEALTH — the monitor of the monitors. A surveillance
# system's worst failure is a silently dead source. This reports,
# per source, the last successful run, the last time it returned
# any rows, and a stale flag (no successful fetch in > stale_days).
# The R pipeline writes fetch_log rows each run.
# ============================================================
@app.get("/api/source_health")
def source_health(stale_days: int = 3, user: str = Depends(require_user)):
    con = db()
    try:
        rows = con.execute("""
            SELECT source, track,
                   MAX(CASE WHEN ok=1 THEN run_at END)              AS last_ok,
                   MAX(CASE WHEN ok=1 AND n_fetched>0 THEN run_at END) AS last_nonempty,
                   SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END)            AS error_runs,
                   COUNT(*)                                          AS total_runs
              FROM fetch_log
             GROUP BY source, track
             ORDER BY track, source""").fetchall()
        now = datetime.now(timezone.utc)
        out = []
        for r in rows:
            stale = True
            if r["last_ok"]:
                try:
                    last = datetime.fromisoformat(r["last_ok"].replace(" ", "T")).replace(tzinfo=timezone.utc)
                    stale = (now - last).days >= stale_days
                except Exception:
                    pass
            out.append({"source": r["source"], "track": r["track"],
                        "last_ok": r["last_ok"], "last_nonempty": r["last_nonempty"],
                        "error_runs": r["error_runs"], "total_runs": r["total_runs"],
                        "stale": stale})
        return {"stale_days": stale_days, "sources": out}
    finally:
        con.close()

@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"


# Each process bounds password and MFA attempts per direct client address.
# The deployment uses one web worker; trusted proxy configuration determines
# request.client. Limits are intentionally not based on spoofable raw headers.
_AUTH_LIMIT = sec.AttemptLimiter(limit=20, window_seconds=300)

def _limit_auth(request, stage):
    host = request.client.host if request.client else "unknown"
    wait = _AUTH_LIMIT.retry_after((stage, host))
    if wait:
        raise HTTPException(429, "Too many authentication attempts; try again later",
                            headers={"Retry-After": str(wait)})


# ================= AUTH: SETUP / LOGIN / 2FA ==================
@app.get("/setup", response_class=HTMLResponse)
def setup_form(request: Request):
    con = db(); admin = get_admin(con); con.close()
    if admin:
        return RedirectResponse("/login", 303)
    return render("setup.html", error=None)

@app.post("/setup")
def setup_submit(username: str = Form(...), password: str = Form(...),
                 password2: str = Form(...)):
    con = db()
    if get_admin(con):
        con.close(); return RedirectResponse("/login", 303)
    if len(password) < 10 or len(password.encode("utf-8")) > 72 or password != password2:
        con.close()
        return render("setup.html",
                      error="Passwords must match, contain at least 10 characters, and fit within 72 UTF-8 bytes.")
    con.execute("INSERT INTO auth(id,username,pw_hash) VALUES(1,?,?)",
                (username, sec.hash_pw(password)))
    con.commit(); con.close()
    return RedirectResponse("/login", 303)

@app.get("/login", response_class=HTMLResponse)
def login_form():
    con = db(); admin = get_admin(con); con.close()
    if not admin:
        return RedirectResponse("/setup", 303)
    return render("login.html", error=None)

@app.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    _limit_auth(request, "password")
    con = db()
    admin = get_admin(con)
    if not admin or admin["username"] != username or \
       not sec.verify_pw(password, admin["pw_hash"]):
        con.close()
        return render("login.html", error="Invalid credentials.")
    token = sec.new_session_token()
    # 2FA is mandatory. A session is never mfa_passed at login time:
    #   - if TOTP is enrolled, user must enter a code at /2fa
    #   - if NOT yet enrolled, /2fa routes them to /2fa/enroll first
    con.execute("INSERT INTO sessions(token,expires_at,mfa_passed) VALUES(?,?,0)",
                (token, sec.session_expiry()))
    con.commit(); con.close()
    resp = RedirectResponse("/2fa", 303)
    set_session_cookie(resp, token)
    return resp

@app.get("/2fa", response_class=HTMLResponse)
def twofa_form(request: Request):
    token = request.cookies.get(COOKIE)
    con = db(); s = current_session(con, token); admin = get_admin(con); con.close()
    if not s:
        return RedirectResponse("/login", 303)
    if not admin["totp_enabled"]:
        return RedirectResponse("/2fa/enroll", 303)
    return render("twofa.html", error=None)

@app.post("/2fa")
def twofa_submit(request: Request, code: str = Form(...)):
    _limit_auth(request, "mfa")
    token = request.cookies.get(COOKIE)
    con = db()
    con.execute("BEGIN IMMEDIATE")
    s = current_session(con, token); admin = get_admin(con)
    if not s:
        con.close(); return RedirectResponse("/login", 303)
    if not admin or not admin["totp_enabled"]:
        con.close(); return RedirectResponse("/2fa/enroll", 303)
    ok = sec.verify_totp(admin["totp_secret"], code)
    if not ok:
        # try recovery code
        good, new_json = sec.consume_recovery_code(code, admin["recovery_codes"])
        if good:
            con.execute("UPDATE auth SET recovery_codes=? WHERE id=1", (new_json,))
            ok = True
    if not ok:
        con.commit(); con.close()
        return render("twofa.html", error="Invalid code.")
    con.execute("UPDATE sessions SET mfa_passed=1 WHERE token=?", (token,))
    con.commit(); con.close()
    return RedirectResponse("/", 303)

@app.get("/2fa/enroll", response_class=HTMLResponse)
def enroll_form(request: Request):
    token = request.cookies.get(COOKIE)
    con = db()
    con.execute("BEGIN IMMEDIATE")
    s = current_session(con, token); admin = get_admin(con)
    if not s:
        con.close(); return RedirectResponse("/login", 303)
    # Enrollment is a one-time transition. A password-only session must never
    # reveal the existing second factor, even by directly visiting this URL.
    if admin["totp_enabled"]:
        con.close()
        return RedirectResponse("/" if s["mfa_passed"] else "/2fa", 303)
    # generate a secret if none pending
    secret = admin["totp_secret"] or sec.new_totp_secret()
    if not admin["totp_secret"]:
        con.execute("UPDATE auth SET totp_secret=? WHERE id=1", (secret,))
        con.commit()
    con.close()
    uri = sec.totp_uri(secret, admin["username"])
    return render("enroll.html", secret=secret, uri=uri, error=None, codes=None)

@app.post("/2fa/enroll")
def enroll_submit(request: Request, code: str = Form(...)):
    _limit_auth(request, "mfa")
    token = request.cookies.get(COOKIE)
    con = db()
    con.execute("BEGIN IMMEDIATE")
    s = current_session(con, token); admin = get_admin(con)
    if not s:
        con.close(); return RedirectResponse("/login", 303)
    if admin["totp_enabled"]:
        con.close()
        raise HTTPException(409, "Two-factor authentication is already enrolled")
    if not admin["totp_secret"]:
        con.close(); return RedirectResponse("/2fa/enroll", 303)
    if not sec.verify_totp(admin["totp_secret"], code):
        uri = sec.totp_uri(admin["totp_secret"], admin["username"])
        con.close()
        return render("enroll.html", secret=admin["totp_secret"], uri=uri,
                      error="Code didn't match — check your authenticator.", codes=None)
    codes = sec.gen_recovery_codes()
    con.execute("UPDATE auth SET totp_enabled=1, recovery_codes=? WHERE id=1",
                (json.dumps(sec.hash_codes(codes)),))
    con.execute("UPDATE sessions SET mfa_passed=1 WHERE token=?", (token,))
    con.commit(); con.close()
    # show recovery codes ONCE
    uri = sec.totp_uri(admin["totp_secret"], admin["username"])
    return render("enroll.html", secret=None, uri=uri, error=None, codes=codes)

@app.post("/logout")
def logout(request: Request):
    token = request.cookies.get(COOKIE)
    con = db(); con.execute("DELETE FROM sessions WHERE token=?", (token,))
    con.commit(); con.close()
    resp = RedirectResponse("/login", 303)
    resp.delete_cookie(COOKIE)
    return resp


# ================= PROTECTED: DASHBOARD + API =================
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: str = Depends(require_user)):
    con = db()
    rows = con.execute("SELECT id,label,enabled,email_enabled FROM questions ORDER BY label").fetchall()
    con.close()
    qs = [dict(r) for r in rows]
    return render("dashboard.html", questions=qs, base_url=BASE_URL,
                  user=user, sources=SOURCES, sources_with_keys=SOURCES_WITH_KEYS,
                  study_types=list(STUDY_LABELS.keys()),
                  signal_sources=SIGNAL_SOURCES,
                  signal_key_sources=SIGNAL_KEY_SOURCES,
                  feed_categories=FEED_CATEGORIES)


class Concept(BaseModel):
    name: str = Field(min_length=1)
    terms: list[str] = Field(default_factory=list)
    mesh: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_concept(self):
        if not self.name.strip() or not any(x.strip() for x in self.terms + self.mesh):
            raise ValueError("Each concept needs a name and at least one term or MeSH heading")
        if any(not x.strip() for x in self.terms + self.mesh):
            raise ValueError("Concept terms cannot be blank")
        return self


class Question(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$", max_length=128)
    label: str = Field(min_length=1)
    concepts: list[Concept] = Field(min_length=1)
    study_filter: list[str] = Field(default_factory=list)
    from_days_back: int = Field(default=7, ge=1, strict=True)
    enabled: StrictBool = True
    sources: dict[str, StrictBool] = Field(default_factory=dict)
    api_keys: dict[str, StrictStr] = Field(default_factory=dict)
    delivery: list[Literal["file", "zotero"]] = Field(default_factory=lambda: ["file"])
    email_enabled: StrictBool = False
    email_cron: str = "0 7 * * 1"
    email_to: list[str] = Field(default_factory=list)
    digest_mode: Literal["all", "threshold"] = "all"
    min_score: float = Field(default=0.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_question(self):
        if not self.label.strip():
            raise ValueError("Question label cannot be blank")
        for values, known, label in ((self.sources, SOURCES, "source"),
                                     (self.api_keys, SOURCES_WITH_KEYS, "API key source")):
            unknown = set(values) - set(known)
            if unknown:
                raise ValueError(f"Unknown {label}: {', '.join(sorted(unknown))}")
        if len(self.email_cron.split()) != 5 or not croniter.is_valid(self.email_cron):
            raise ValueError("email_cron must be a valid five-field cron expression")
        if self.email_enabled and not self.email_to:
            raise ValueError("Email delivery requires at least one recipient")
        for address in self.email_to:
            if (not address.strip() or "@" not in address or
                    any(c.isspace() or ord(c) < 32 for c in address)):
                raise ValueError("Each email recipient must be one address without whitespace")
        return self


# ============================================================
# INGEST ENDPOINT — accepts normalized records/signals from n8n
# (or another external normalizer) and runs them through the SAME
# dedup + storage path as the R pipeline. The external producer handles normalization
# (unstructured email/RSS -> fields); R keeps ownership of the
# load-bearing classification + ranking (ingested records default to
# 'unclassified' and are (re)classified on the next R run).
# ============================================================
import re as _re

def _norm_doi(x: str) -> str:
    # EXACT mirror of R .norm_doi (R/03_dedup.R): lower, trim, strip doi.org prefix
    x = (x or "").strip().lower()
    x = _re.sub(r"^https?://(dx\.)?doi\.org/", "", x)
    return x

def _norm_title(x: str) -> str:
    # EXACT mirror of R .norm_title: lower, non-alnum->space, collapse spaces, trim
    x = (x or "").lower()
    x = _re.sub(r"[^a-z0-9 ]", " ", x)
    x = _re.sub(r"\s+", " ", x)
    return x.strip()

def _record_dedup_key(doi: str, title: str, year: str) -> str:
    # Mirror R .collapse_batch. Preserve non-ASCII scientific distinctions
    # (e.g. alpha/beta symbols) instead of stripping them into the same key.
    dn = _norm_doi(doi)
    if dn:
        return f"doi:{dn}"
    if any(ord(c) > 127 for c in (title or "")):
        exact_title = _re.sub(r"[ \t\r\n]+", " ", title).strip(" ")
        return f"tiu:{exact_title}|{year or ''}"
    return f"ti:{_norm_title(title)}|{year or ''}"

def _signal_dedup_key(source: str, ext_id: str) -> str:
    # EXACT mirror of R .norm_extid + persist_signals: source:<alnum-lowered ext_id>
    norm = _re.sub(r"[^a-z0-9]", "", (ext_id or "").strip().lower())
    return f"{source}:{norm}"

def require_ingest_token(request: Request):
    """Machine auth for /api/ingest. Bearer token must match INGEST_TOKEN."""
    if not INGEST_TOKEN:
        raise HTTPException(503, "Ingest endpoint disabled (no INGEST_TOKEN set)")
    auth = request.headers.get("authorization", "")
    expected = f"Bearer {INGEST_TOKEN}"
    # constant-time compare to avoid token leakage via timing
    import hmac
    if not hmac.compare_digest(auth, expected):
        raise HTTPException(401, "Invalid or missing ingest token")
    return True


def _normalize_ingest_date(value):
    """Store date fields in one sortable calendar form, as the R writer does."""
    value = value.strip()
    if not value:
        return ""
    try:
        if _re.fullmatch(r"[0-9]{4}", value):
            return date(int(value), 1, 1).isoformat()
        if _re.fullmatch(r"[0-9]{4}[-/][0-9]{1,2}", value):
            year, month = _re.split(r"[-/]", value)
            return date(int(year), int(month), 1).isoformat()
        if _re.fullmatch(r"[0-9]{8}", value):
            return datetime.strptime(value, "%Y%m%d").date().isoformat()
        if _re.fullmatch(r"[0-9]{4}/[0-9]{1,2}/[0-9]{1,2}", value):
            return date(*(int(part) for part in value.split("/"))).isoformat()
        if len(value) == 10:
            return date.fromisoformat(value).isoformat()
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError as exc:
        raise ValueError("Date must be a valid ISO date/timestamp, YYYYMMDD, year or year-month") from exc

class IngestRecord(BaseModel):
    question_id: str
    title: str
    source: str = "ingest"          # e.g. ingest:scholar, ingest:ir
    doi: str = ""
    abstract: str = ""
    authors: str = ""
    journal: str = ""
    pub_date: str = ""              # YYYY-MM-DD preferred
    url: str = ""
    oa_url: str = ""
    type: str = ""                  # raw publication-type hint, if any
    study_type: str = ""            # optional producer-supplied hint; '' -> 'unclassified'
    is_preprint: int = 0
    score: float = Field(default=0.0, allow_inf_nan=False)

    @field_validator("pub_date")
    @classmethod
    def normalize_date(cls, value):
        return _normalize_ingest_date(value)

VALID_CONFIDENCE = {"authoritative", "reported", "unverified"}
INGEST_MAX_SCORE = 10.0   # posted scores are capped; R rescoring is canonical

class IngestSignal(BaseModel):
    question_id: str
    title: str
    signal_type: str                # grant|trial_*|regulatory|sec_filing|patent|press_release|conference|ir_alert|literature_alert|institution|aggregator|trade_press|news
    source: str = "ingest"
    ext_id: str = ""
    summary: str = ""
    org: str = ""
    signal_date: str = ""
    url: str = ""
    confidence: str = "reported"    # authoritative|reported|unverified
    extra: dict = {}
    score: float = Field(default=0.0, allow_inf_nan=False)

    @field_validator("signal_date")
    @classmethod
    def normalize_date(cls, value):
        return _normalize_ingest_date(value)


def _same_json(left, right):
    # Python considers True == 1; JSON and R preserve that type distinction.
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, dict) or isinstance(right, dict):
        return (isinstance(left, dict) and isinstance(right, dict)
                and left.keys() == right.keys()
                and all(_same_json(left[key], right[key]) for key in left))
    if isinstance(left, list) or isinstance(right, list):
        return (isinstance(left, list) and isinstance(right, list)
                and len(left) == len(right)
                and all(_same_json(a, b) for a, b in zip(left, right)))
    return left == right


def _signal_revision_key(con, base, question_id, values):
    """Preserve changed signals as append-only events, shared with the R writer."""
    prefix = base + ":revision:"
    previous = con.execute(
        "SELECT * FROM signals WHERE question_id=? AND "
        "(dedup_key=? OR substr(dedup_key,1,?)=?)",
        (question_id, base, len(prefix), prefix)).fetchall()
    fields = ("source", "ext_id", "signal_type", "title", "summary", "org",
              "signal_date", "url", "confidence")
    for row in previous:
        if not all(str(row[field] or "") == str(values.get(field) or "") for field in fields):
            continue
        try:
            same_extra = _same_json(json.loads(row["extra"] or "{}"), values["extra"])
        except (ValueError, TypeError):
            same_extra = False
        if same_extra:
            return None
    used = {row["dedup_key"] for row in previous}
    if base not in used:
        return base
    revision = 1
    while prefix + str(revision) in used:
        revision += 1
    return prefix + str(revision)


class IngestPayload(BaseModel):
    records: list[IngestRecord] = []
    signals: list[IngestSignal] = []

@app.post("/api/ingest")
def ingest(payload: IngestPayload, request: Request, _: bool = Depends(require_ingest_token)):
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        question_channels = {r["id"]: r["delivery"]
                             for r in con.execute("SELECT id,delivery FROM questions")}
        valid_q = set(question_channels)
        rec_new = rec_dup = sig_new = sig_dup = 0
        skipped = []

        for r in payload.records:
            if r.question_id not in valid_q:
                skipped.append({"title": r.title[:60], "reason": "unknown question_id"})
                continue
            year = (r.pub_date or "")[:4]
            key = _record_dedup_key(r.doi, r.title, year)
            exists = con.execute(
                "SELECT 1 FROM records WHERE dedup_key=? AND question_id=?",
                (key, r.question_id)).fetchone()
            if exists:
                rec_dup += 1
                continue
            con.execute("""INSERT INTO records(dedup_key,question_id,source,ext_id,doi,
                title,abstract,authors,journal,pub_date,url,oa_url,type,study_type,
                is_preprint,score,first_seen,emailed)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),0)""",
                (key, r.question_id, r.source, "", r.doi, r.title, r.abstract,
                 r.authors, r.journal, r.pub_date, r.url, r.oa_url, r.type,
                 r.study_type or "unclassified", int(r.is_preprint),
                 min(float(r.score), INGEST_MAX_SCORE)))
            # Queue alongside the record. A failure rolls both back, and an
            # identical ingestion retry cannot replay previously seen records.
            channels = json.loads(question_channels[r.question_id] or "[]")
            for channel in ("file", "zotero"):
                if channel in channels:
                    con.execute("INSERT OR IGNORE INTO delivery_outbox"
                                "(channel,question_id,dedup_key) VALUES(?,?,?)",
                                (channel, r.question_id, key))
            rec_new += 1

        for s in payload.signals:
            if s.question_id not in valid_q:
                skipped.append({"title": s.title[:60], "reason": "unknown question_id"})
                continue
            if s.signal_type not in SIGNAL_TYPES:
                skipped.append({"title": s.title[:60],
                                "reason": f"invalid signal_type '{s.signal_type}'"})
                continue
            if s.confidence not in VALID_CONFIDENCE:
                skipped.append({"title": s.title[:60],
                                "reason": f"invalid confidence '{s.confidence}'"})
                continue
            s.ext_id = s.ext_id or s.url or s.title
            base = _signal_dedup_key(s.source, s.ext_id)
            key = _signal_revision_key(con, base, s.question_id, s.model_dump())
            if key is None:
                sig_dup += 1
                continue
            con.execute("""INSERT INTO signals(dedup_key,question_id,signal_type,source,
                ext_id,title,summary,org,signal_date,url,confidence,extra,score,
                first_seen,emailed)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),0)""",
                (key, s.question_id, s.signal_type, s.source, s.ext_id, s.title,
                 s.summary, s.org, s.signal_date, s.url, s.confidence,
                 json.dumps(s.extra, sort_keys=True), min(float(s.score), INGEST_MAX_SCORE)))
            sig_new += 1

        con.commit()
        return {"ok": True,
                "records": {"inserted": rec_new, "duplicates": rec_dup},
                "signals": {"inserted": sig_new, "duplicates": sig_dup},
                "skipped": skipped}
    finally:
        con.close()

@app.get("/api/questions")
def list_questions(user: str = Depends(require_user)):
    con = db(); out = []
    for q in con.execute("SELECT * FROM questions ORDER BY label").fetchall():
        cons = con.execute(
            "SELECT name,terms,mesh FROM concepts WHERE question_id=? ORDER BY position",
            (q["id"],)).fetchall()
        # report WHICH sources have a stored key, never the key itself
        try:
            keymap = json.loads(q["api_keys_enc"] or "{}")
        except json.JSONDecodeError:
            keymap = {}
        out.append({
            "id": q["id"], "label": q["label"],
            "study_filter": json.loads(q["study_filter"] or "[]"),
            "from_days_back": q["from_days_back"],
            "enabled": bool(q["enabled"]),
            "sources": json.loads(q["sources"] or "{}"),
            "api_key_present": {k: bool(v) for k, v in keymap.items()},
            "delivery": json.loads(q["delivery"] or "[]"),
            "email_enabled": bool(q["email_enabled"]),
            "email_cron": q["email_cron"],
            "email_to": json.loads(q["email_to"] or "[]"),
            "email_last_sent": q["email_last_sent"],
            "digest_mode": (q["digest_mode"] if "digest_mode" in q.keys() else "all"),
            "min_score": (q["min_score"] if "min_score" in q.keys() else 0),
            "concepts": [{"name": c["name"],
                          "terms": json.loads(c["terms"] or "[]"),
                          "mesh": json.loads(c["mesh"] or "[]")} for c in cons],
        })
    con.close()
    return out

@app.post("/api/questions")
def upsert_question(q: Question, user: str = Depends(require_user)):
    con = db()
    try:
        # merge API keys: keep existing encrypted keys, overwrite only those
        # the user supplied plaintext for this time (blank = leave as-is).
        existing = con.execute("SELECT api_keys_enc FROM questions WHERE id=?",
                               (q.id,)).fetchone()
        keymap = {}
        if existing and existing["api_keys_enc"]:
            try: keymap = json.loads(existing["api_keys_enc"])
            except json.JSONDecodeError: keymap = {}
        for src, plain in (q.api_keys or {}).items():
            if plain == "__CLEAR__":
                keymap.pop(src, None)
            elif plain:
                keymap[src] = sec.encrypt_secret(plain)
        api_keys_enc = json.dumps(keymap)

        con.execute("DELETE FROM concepts WHERE question_id=?", (q.id,))
        con.execute("""
            INSERT INTO questions(id,label,study_filter,from_days_back,enabled,
              sources,api_keys_enc,delivery,email_enabled,email_cron,email_to,
              digest_mode,min_score,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
              label=excluded.label, study_filter=excluded.study_filter,
              from_days_back=excluded.from_days_back, enabled=excluded.enabled,
              sources=excluded.sources, api_keys_enc=excluded.api_keys_enc,
              delivery=excluded.delivery, email_enabled=excluded.email_enabled,
              email_cron=excluded.email_cron, email_to=excluded.email_to,
              digest_mode=excluded.digest_mode, min_score=excluded.min_score,
              updated_at=datetime('now')
        """, (q.id, q.label, json.dumps(q.study_filter), q.from_days_back,
              int(q.enabled), json.dumps(q.sources), api_keys_enc,
              json.dumps(q.delivery), int(q.email_enabled), q.email_cron,
              json.dumps(q.email_to), q.digest_mode, float(q.min_score)))
        for i, c in enumerate(q.concepts):
            con.execute(
                "INSERT INTO concepts(question_id,name,position,terms,mesh) VALUES(?,?,?,?,?)",
                (q.id, c.name, i, json.dumps(c.terms), json.dumps(c.mesh)))
        con.commit()
    finally:
        con.close()
    return {"ok": True, "id": q.id}

@app.delete("/api/questions/{qid}")
def delete_question(qid: str, user: str = Depends(require_user)):
    con = db(); con.execute("DELETE FROM questions WHERE id=?", (qid,))
    con.commit(); con.close()
    return {"ok": True, "deleted": qid}


# ============================================================
# /api/v1 — machine-readable surface for the researchR R client.
#
# Auth is a bearer token, NOT the dashboard session: the dashboard is a
# browser flow gated on TOTP, which a package cannot carry. Unset API_TOKEN
# disables the surface entirely (503) so it can never serve unauthenticated.
#
# Read endpoints return JSON rows with the same field names the R client
# turns into data frame columns. API keys are never returned here either --
# only the per-source "present" booleans, exactly as on /api/questions.
# ============================================================

def require_api_token(request: Request):
    if not API_TOKEN:
        raise HTTPException(503, "API disabled (no API_TOKEN set)")
    import hmac
    if not hmac.compare_digest(request.headers.get("authorization", ""),
                               f"Bearer {API_TOKEN}"):
        raise HTTPException(401, "Invalid or missing API token")
    return True


API = Depends(require_api_token)

RECORD_FIELDS = ["dedup_key", "question_id", "source", "ext_id", "doi", "title",
                 "abstract", "authors", "journal", "pub_date", "url", "oa_url",
                 "type", "study_type", "is_preprint", "score", "first_seen",
                 "emailed", "provenance"]
SIGNAL_FIELDS = ["dedup_key", "question_id", "signal_type", "source", "ext_id",
                 "title", "summary", "org", "signal_date", "url", "confidence",
                 "extra", "score", "first_seen", "emailed"]
CONFIDENCE_ORDER = ["unverified", "reported", "authoritative"]
MAX_PAGE = 1000


def _page(limit: int, offset: int):
    """Clamp paging so a client cannot ask for the whole table at once."""
    return max(1, min(int(limit), MAX_PAGE)), max(0, int(offset))


def _require_question(con, qid):
    if not con.execute("SELECT 1 FROM questions WHERE id=?", (qid,)).fetchone():
        con.close()
        raise HTTPException(404, "No such question")


@app.get("/api/v1/meta")
def v1_meta(_: bool = API):
    """Vocabulary the client validates against, so it cannot drift from the
    server's taxonomy without noticing."""
    return {"api_version": 1,
            "study_types": list(STUDY_LABELS.keys()),
            "study_labels": STUDY_LABELS,
            "signal_types": SIGNAL_TYPES,
            "confidence_levels": CONFIDENCE_ORDER,
            "sources": SOURCES,
            "sources_with_keys": SOURCES_WITH_KEYS,
            "max_page": MAX_PAGE}


@app.get("/api/v1/questions")
def v1_questions(_: bool = API):
    return list_questions(user="api")


@app.post("/api/v1/questions")
def v1_upsert_question(q: Question, _: bool = API):
    return upsert_question(q, user="api")


@app.delete("/api/v1/questions/{qid}")
def v1_delete_question(qid: str, _: bool = API):
    return delete_question(qid, user="api")


@app.get("/api/v1/records")
def v1_records(question_id: str, study_type: str | None = None,
               is_preprint: bool | None = None, since: str | None = None,
               until: str | None = None, min_score: float | None = None,
               emailed: bool | None = None, apply_threshold: bool = False,
               limit: int = 200, offset: int = 0, _: bool = API):
    limit, offset = _page(limit, offset)
    con = db()
    try:
        _require_question(con, question_id)
        where, params = ["question_id=?"], [question_id]
        if study_type is not None:
            if study_type not in STUDY_LABELS:
                raise HTTPException(400, "Unknown study_type")
            where.append("study_type=?"); params.append(study_type)
        if is_preprint is not None:
            where.append("is_preprint=?"); params.append(int(is_preprint))
        if emailed is not None:
            where.append("emailed=?"); params.append(int(emailed))
        if since:
            where.append("pub_date >= ?"); params.append(since)
        if until:
            where.append("pub_date <= ?"); params.append(until)
        if min_score is not None:
            where.append("score >= ?"); params.append(float(min_score))
        if apply_threshold:
            q = con.execute("SELECT digest_mode,min_score FROM questions WHERE id=?",
                            (question_id,)).fetchone()
            if q and (q["digest_mode"] or "all") == "threshold":
                where.append("score >= ?"); params.append(float(q["min_score"] or 0))
        clause = " AND ".join(where)
        total = con.execute(f"SELECT COUNT(*) c FROM records WHERE {clause}",
                            params).fetchone()["c"]
        rows = con.execute(
            f"SELECT {','.join(RECORD_FIELDS)} FROM records WHERE {clause} "
            f"ORDER BY first_seen DESC, score DESC, dedup_key ASC LIMIT ? OFFSET ?",
            (*params, limit, offset)).fetchall()
        return {"total": total, "limit": limit, "offset": offset,
                "records": [dict(r) for r in rows]}
    finally:
        con.close()


@app.get("/api/v1/signals")
def v1_signals(question_id: str, signal_type: str | None = None,
               min_confidence: str | None = None, since: str | None = None,
               limit: int = 200, offset: int = 0, _: bool = API):
    limit, offset = _page(limit, offset)
    con = db()
    try:
        _require_question(con, question_id)
        where, params = ["question_id=?"], [question_id]
        if signal_type is not None:
            if signal_type not in SIGNAL_TYPES:
                raise HTTPException(400, "Unknown signal_type")
            where.append("signal_type=?"); params.append(signal_type)
        if min_confidence is not None:
            if min_confidence not in CONFIDENCE_ORDER:
                raise HTTPException(400, "Unknown confidence level")
            allowed = CONFIDENCE_ORDER[CONFIDENCE_ORDER.index(min_confidence):]
            where.append(f"confidence IN ({','.join('?' * len(allowed))})")
            params.extend(allowed)
        if since:
            where.append("signal_date >= ?"); params.append(since)
        clause = " AND ".join(where)
        total = con.execute(f"SELECT COUNT(*) c FROM signals WHERE {clause}",
                            params).fetchone()["c"]
        rows = con.execute(
            f"SELECT {','.join(SIGNAL_FIELDS)} FROM signals WHERE {clause} "
            f"ORDER BY signal_date DESC, score DESC, dedup_key ASC LIMIT ? OFFSET ?",
            (*params, limit, offset)).fetchall()
        return {"total": total, "limit": limit, "offset": offset,
                "signals": [dict(r) for r in rows]}
    finally:
        con.close()


@app.get("/api/v1/source_health")
def v1_source_health(stale_days: int = 3, _: bool = API):
    return source_health(stale_days=stale_days, user="api")


@app.get("/api/v1/signal_config/{qid}")
def v1_get_signal_config(qid: str, _: bool = API):
    return get_signal_config(qid, user="api")


@app.post("/api/v1/signal_config/{qid}")
def v1_upsert_signal_config(qid: str, cfg: SignalConfig, _: bool = API):
    return upsert_signal_config(qid, cfg, user="api")


# ---- service liveness ----------------------------------------
def _heartbeat_rows():
    con = db()
    try:
        rows = con.execute(
            "SELECT service,last_run,last_ok,last_detail,last_error "
            "FROM service_heartbeat ORDER BY service").fetchall()
        out = []
        now = datetime.now(timezone.utc)
        for r in rows:
            stale = True
            if r["last_ok"]:
                try:
                    last = datetime.fromisoformat(
                        r["last_ok"].replace(" ", "T")).replace(tzinfo=timezone.utc)
                    stale = (now - last).total_seconds() > 36 * 3600
                except Exception:
                    pass
            out.append({**dict(r), "stale": stale})
        return {"services": out, "stale_after_hours": 36}
    finally:
        con.close()


@app.get("/api/heartbeat")
def api_heartbeat(user: str = Depends(require_user)):
    return _heartbeat_rows()


@app.get("/api/v1/heartbeat")
def v1_heartbeat(_: bool = API):
    return _heartbeat_rows()
