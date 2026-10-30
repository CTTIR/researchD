-- ============================================================
-- schema.sql  — shared database for the R engine and web service.
-- Questions live in the DB (editable from the web UI). WAL mode
-- lets the R writer and the web service coexist.
-- ============================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- One row per research question.
CREATE TABLE IF NOT EXISTS questions (
  id             TEXT PRIMARY KEY,         -- slug, e.g. "glp1_mash"
  label          TEXT NOT NULL,
  study_filter   TEXT DEFAULT '[]',        -- JSON array of pub types (optional)
  from_days_back INTEGER DEFAULT 7,
  -- PRECISION CONTROL (recall safety-valve model):
  -- records are always STORED (nothing dropped). These control what reaches
  -- the *digest feed* and *email* — the high-signal stream you actually read.
  -- The unfiltered "/feed/{id}/all.xml" always shows everything.
  --   digest_mode: 'threshold' (score >= min_score) | 'all' (no filtering)
  --   min_score:   cutoff used when digest_mode = 'threshold'
  digest_mode    TEXT DEFAULT 'all',
  min_score      REAL DEFAULT 0,
  enabled        INTEGER DEFAULT 1,
  -- per-question source selection: JSON object e.g.
  --   {"pubmed":true,"europepmc":true,"openalex":false,
  --    "clinicaltrials":true,"biorxiv":false}
  sources        TEXT DEFAULT '{}',
  -- per-question encrypted API keys: JSON object of {source: fernet_token}
  -- e.g. {"pubmed":"gAAAAAB...","openalex":"gAAAAAB..."}
  api_keys_enc   TEXT DEFAULT '{}',
  -- email distribution
  email_enabled  INTEGER DEFAULT 0,
  email_cron     TEXT DEFAULT '0 7 * * 1', -- this question's own cadence
  email_to       TEXT DEFAULT '[]',        -- JSON array of recipient addresses
  email_last_sent TEXT,                     -- ISO timestamp of last successful send
  -- other delivery channels (file, zotero) kept as a JSON array
  delivery       TEXT DEFAULT '["file"]',
  created_at     TEXT DEFAULT (datetime('now')),
  updated_at     TEXT DEFAULT (datetime('now'))
);

-- Concepts belonging to a question (AND-joined at query time).
CREATE TABLE IF NOT EXISTS concepts (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  question_id TEXT NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
  name        TEXT NOT NULL,
  position    INTEGER DEFAULT 0,
  terms       TEXT DEFAULT '[]',           -- JSON array of synonyms
  mesh        TEXT DEFAULT '[]'            -- JSON array of MeSH headings
);
CREATE INDEX IF NOT EXISTS idx_concepts_q ON concepts(question_id);

-- Retrieved records.
CREATE TABLE IF NOT EXISTS records (
  dedup_key   TEXT NOT NULL,
  question_id TEXT NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
  source      TEXT,
  ext_id      TEXT,
  doi         TEXT,
  title       TEXT,
  abstract    TEXT,
  authors     TEXT,
  journal     TEXT,
  pub_date    TEXT,
  url         TEXT,
  oa_url      TEXT,                        -- free open-access full-text (Unpaywall), if any
  provenance  TEXT DEFAULT '[]',           -- source identities and fields used in metadata merging
  type        TEXT,
  study_type  TEXT,
  is_preprint INTEGER DEFAULT 0,
  score       REAL,
  first_seen  TEXT DEFAULT (datetime('now')),
  emailed     INTEGER DEFAULT 0,           -- has this record been sent yet?
  PRIMARY KEY (dedup_key, question_id)
);
CREATE INDEX IF NOT EXISTS idx_records_q_date ON records(question_id, pub_date DESC);
CREATE INDEX IF NOT EXISTS idx_records_q_seen ON records(question_id, first_seen DESC);
CREATE INDEX IF NOT EXISTS idx_records_q_emailed ON records(question_id, emailed);

-- Durable delivery state. Only new records are queued when Zotero delivery is
-- enabled; upgrading must not replay an existing library's historical records.
CREATE TABLE IF NOT EXISTS delivery_outbox (
  channel     TEXT NOT NULL,
  question_id TEXT NOT NULL,
  dedup_key   TEXT NOT NULL,
  state       TEXT NOT NULL DEFAULT 'pending',
  attempts    INTEGER NOT NULL DEFAULT 0,
  last_error  TEXT DEFAULT '',
  remote_key  TEXT DEFAULT '',
  destination TEXT DEFAULT '',
  updated_at  TEXT DEFAULT (datetime('now')),
  PRIMARY KEY (channel, question_id, dedup_key),
  FOREIGN KEY (dedup_key, question_id) REFERENCES records(dedup_key, question_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_delivery_pending ON delivery_outbox(channel, state, question_id);

-- ============================================================
-- SIGNALS TRACK — parallel to records, for non-paper intelligence.
-- Shares the SAME questions/concepts, but these objects are
-- structurally different from publications (a grant award, a trial
-- registration, an 8-K filing, a patent, a scraped press release).
--
-- signal_type: grant | trial_registration | trial_result |
--              regulatory | sec_filing | patent | press_release |
--              conference
--
-- signal_date MEANING varies by type (award date / registration date /
--   results-posted date / decision date / filing date / priority date /
--   release date) — store the most "this just became public" date.
--
-- confidence: 'authoritative' (SEC, regulator, registry) |
--             'reported'      (official-but-preliminary, e.g. topline) |
--             'unverified'    (scraped press/IR, rumor-tier)
-- This is the "speed vs reliability" axis made explicit per signal.
-- ============================================================
CREATE TABLE IF NOT EXISTS signals (
  dedup_key    TEXT NOT NULL,
  question_id  TEXT NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
  signal_type  TEXT NOT NULL,
  source       TEXT,                  -- sec_edgar | nih_reporter | ctgov |
                                      -- ctis | isrctn | openfda | ema |
                                      -- patentsview | epo_ops | cordis |
                                      -- press:<domain> | conf:<name>
  ext_id       TEXT,                  -- NCT id / application no / accession /
                                      -- patent no / grant id / URL hash
  title        TEXT,
  summary      TEXT,                  -- abstract / brief / topline text
  org          TEXT,                  -- sponsor / company / assignee / awardee
  signal_date  TEXT,                  -- see meaning note above
  url          TEXT,
  confidence   TEXT DEFAULT 'reported',
  extra        TEXT DEFAULT '{}',     -- JSON: type-specific fields (phase,
                                      -- form_type, amount, status, etc.)
  score        REAL,
  first_seen   TEXT DEFAULT (datetime('now')),
  emailed      INTEGER DEFAULT 0,
  PRIMARY KEY (dedup_key, question_id)
);
CREATE INDEX IF NOT EXISTS idx_signals_q_date ON signals(question_id, signal_date DESC);
CREATE INDEX IF NOT EXISTS idx_signals_q_type ON signals(question_id, signal_type);
CREATE INDEX IF NOT EXISTS idx_signals_q_emailed ON signals(question_id, emailed);

-- Per-question signal-source toggles + scraper targets live in their own
-- table (kept separate from `questions` to avoid bloating that row and to
-- allow a variable-length list of scrape targets per question).
CREATE TABLE IF NOT EXISTS signal_config (
  question_id     TEXT PRIMARY KEY REFERENCES questions(id) ON DELETE CASCADE,
  -- which signal sources are enabled: JSON {source: bool}
  sources         TEXT DEFAULT '{}',
  -- which curated-feed CATEGORIES are enabled: JSON {category: bool}
  -- categories: literature_alert, institution, aggregator, trade_press, news
  -- (empty = all enabled)
  feed_categories TEXT DEFAULT '{}',
  -- minimum confidence to include in the signals digest/email:
  --   'unverified' (everything) | 'reported' | 'authoritative'
  min_confidence  TEXT DEFAULT 'unverified',
  -- scrape targets: JSON array of {name, url, type:'rss'|'html', selector?}
  -- e.g. company IR pages or conference late-breaker pages
  scrape_targets  TEXT DEFAULT '[]',
  -- instant alerts: 1 = high-priority signals email IMMEDIATELY (next
  -- scheduler tick) instead of waiting for the digest cron. This is the
  -- "be first" tier: trial_result / ir_alert / sec_filing / regulatory /
  -- press_release at confidence >= reported.
  instant_alerts  INTEGER DEFAULT 0,
  -- encrypted per-source keys specific to signals (e.g. EPO OPS key)
  api_keys_enc    TEXT DEFAULT '{}'
);

-- Single admin user: password hash + TOTP secret.
CREATE TABLE IF NOT EXISTS auth (
  id            INTEGER PRIMARY KEY CHECK (id = 1),  -- singleton row
  username      TEXT NOT NULL,
  pw_hash       TEXT NOT NULL,             -- bcrypt
  totp_secret   TEXT,                      -- base32; null until 2FA enrolled
  totp_enabled  INTEGER DEFAULT 0,
  recovery_codes TEXT DEFAULT '[]',        -- JSON array of bcrypt-hashed codes
  created_at    TEXT DEFAULT (datetime('now'))
);

-- Server-side sessions (so we can require 2FA mid-session).
CREATE TABLE IF NOT EXISTS sessions (
  token       TEXT PRIMARY KEY,
  created_at  TEXT DEFAULT (datetime('now')),
  expires_at  TEXT NOT NULL,
  mfa_passed  INTEGER DEFAULT 0
);

-- ============================================================
-- OPERATIONAL: source health heartbeat. Every pipeline run logs one
-- row per (question, source) with the fetch outcome. A surveillance
-- system's worst failure mode is a silently dead source — this makes
-- staleness visible (dashboard/API) instead of invisible.
-- ============================================================
CREATE TABLE IF NOT EXISTS fetch_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  question_id TEXT,
  track       TEXT,               -- 'lit' | 'signals'
  source      TEXT,
  n_fetched   INTEGER,
  ok          INTEGER,            -- 1 = fetch returned (even 0 rows), 0 = errored
  run_at      TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_fetchlog_src ON fetch_log(source, run_at DESC);

-- ============================================================
-- OPERATIONAL: one row per service, overwritten in place.
--
-- fetch_log answers "is this SOURCE still returning anything"; this answers
-- the prior question, "is this SERVICE still running at all". A crashed
-- engine writes no fetch_log rows either, so without a heartbeat the two
-- failures are indistinguishable from a quiet week.
--
-- Deliberately a heartbeat and not a log: bounded at one row per service,
-- so it can never contribute to disk growth.
-- ============================================================
CREATE TABLE IF NOT EXISTS service_heartbeat (
  service     TEXT PRIMARY KEY,   -- 'pipeline' | 'scheduler'
  last_run    TEXT,               -- every attempt, successful or not
  last_ok     TEXT,               -- last attempt that finished cleanly
  last_detail TEXT,               -- short human-readable summary
  last_error  TEXT                -- message from the most recent failure
);
