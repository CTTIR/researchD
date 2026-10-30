# ============================================================
# 07_db_questions.R
# Load questions from SQLite (editable via web UI) and persist
# full records (with study_type) to the shared `records` table.
# Falls back to seeding from questions.yaml if the DB is empty.
# ============================================================

# Additive migrations. schema.sql only CREATEs tables IF NOT EXISTS, so a
# DB from an older deployment never gains new columns from it.
#
# This list MIRRORS `_MIGRATIONS` in web/app.py and must stay identical --
# `dev/tests/db_phase2_migrate.py` asserts that it does. Both services need
# it because either one can be the first to touch an upgraded DB, and
# docker-compose starts `pipeline` BEFORE `web`.
DB_MIGRATIONS <- list(
  list("questions",     "digest_mode",     "TEXT DEFAULT 'all'"),
  list("questions",     "min_score",       "REAL DEFAULT 0"),
  list("signal_config", "feed_categories", "TEXT DEFAULT '{}'"),
  list("signal_config", "instant_alerts",  "INTEGER DEFAULT 0"),
  list("records",       "oa_url",          "TEXT"),
  list("records",       "provenance",      "TEXT DEFAULT '[]'")
)

db_migrate <- function(con) {
  for (m in DB_MIGRATIONS) {
    cols <- DBI::dbGetQuery(con, sprintf("PRAGMA table_info(%s)", m[[1]]))$name
    if (!(m[[2]] %in% cols)) {
      DBI::dbExecute(con, sprintf("ALTER TABLE %s ADD COLUMN %s %s",
                                  m[[1]], m[[2]], m[[3]]))
    }
  }
  invisible(NULL)
}

# Ensure schema exists (idempotent).
# NOTE: this splits the file on ";" at end-of-line rather than parsing SQL.
# That is fine for the current schema (plain CREATE/PRAGMA statements) and is
# verified against the web service's executescript() in
# dev/tests/db_phase2_migrate.py -- but a future trigger or BEGIN...END block
# would need a real statement splitter here.
db_init_schema <- function(con, schema_path = "db/schema.sql") {
  sql <- paste(readLines(schema_path, warn = FALSE), collapse = "\n")
  for (stmt in strsplit(sql, ";\\s*\n")[[1]]) {
    stmt <- trimws(stmt)
    if (nzchar(stmt)) DBI::dbExecute(con, stmt)
  }
  db_migrate(con)
}

# Decrypt one Fernet token by shelling out to the python helper.
# An absent token means no configured credential. A configured but unreadable
# token must fail visibly rather than silently issuing unauthenticated requests.
.decrypt_key <- function(token) {
  if (is.null(token) || !length(token) || identical(token, "")) return("")
  if (!is.character(token) || length(token) != 1L || is.na(token))
    stop("Configured API key has an invalid encrypted value", call. = FALSE)
  helper <- "web/decrypt_key.py"
  if (!file.exists(helper))
    stop("Cannot decrypt configured API key: web/decrypt_key.py is missing", call. = FALSE)
  out <- tryCatch(
    suppressWarnings(system2("python3", helper, input = token, stdout = TRUE, stderr = FALSE)),
    error = function(e) structure(character(0), status = 127L))
  status <- attr(out, "status") %||% 0L
  if (status != 0L || !length(out) || !nzchar(paste(out, collapse = ""))) {
    reason <- switch(as.character(status),
      `2` = "APP_SECRET_KEY is missing; restore the matching master key",
      `3` = "install the Python cryptography dependency before running the engine",
      `4` = "restore the matching APP_SECRET_KEY or replace the stored credential",
      `127` = "the Python decryption helper could not be executed",
      "the decryption helper failed or returned an empty credential")
    stop("Cannot decrypt configured API key: ", reason, call. = FALSE)
  }
  paste(out, collapse = "\n")
}

# JSON parsers may echo the failing input. Report the field, never stored key
# material or private concept text, when a persisted configuration is corrupt.
.read_config_json <- function(value, field, default) {
  tryCatch(jsonlite::fromJSON(value %||% default), error = function(e)
    stop("Invalid JSON in ", field, call. = FALSE))
}

# Read all enabled questions from the DB into the list shape the
# pipeline expects, including per-question sources + decrypted keys.
load_questions_db <- function(con, selected_ids = character(0)) {
  qs <- DBI::dbGetQuery(con, "SELECT * FROM questions WHERE enabled = 1")
  if (length(selected_ids)) qs <- qs[qs$id %in% selected_ids, , drop = FALSE]
  failures <- list()
  # One unloadable question must not sink the run (ACCEPTANCE C3). The JSON
  # columns are written by the web UI and are normally well-formed, but a
  # hand-edited row, a partially-applied migration or a future schema change
  # would otherwise abort the whole cycle here -- before ANY question runs.
  out <- lapply(seq_len(nrow(qs)), function(i) {
    tryCatch(.load_one_question(con, qs[i, ]), error = function(e) {
      failures[[qs$id[i]]] <<- conditionMessage(e)
      message("  [WARN] skipping unloadable question '", qs$id[i], "': ",
              conditionMessage(e))
      NULL
    })
  })
  out <- Filter(Negate(is.null), out)
  attr(out, "load_errors") <- failures
  out
}

.load_one_question <- function(con, q) {
  local({
    cons <- DBI::dbGetQuery(con,
      "SELECT * FROM concepts WHERE question_id = ? ORDER BY position",
      params = list(q$id))
    concepts <- lapply(seq_len(nrow(cons)), function(j) {
      list(
        name  = cons$name[j],
        terms = .read_config_json(cons$terms[j], "concepts.terms", "[]"),
        mesh  = .read_config_json(cons$mesh[j], "concepts.mesh", "[]")
      )
    })
    # per-question source toggles (default: all on if unspecified)
    src_obj <- .read_config_json(q$sources, "questions.sources", "{}")
    # decrypt per-source API keys
    key_obj_enc <- .read_config_json(q$api_keys_enc, "questions.api_keys_enc", "{}")
    api_keys <- lapply(key_obj_enc, .decrypt_key)

    list(
      id = q$id, label = q$label,
      concepts = concepts,
      study_filter = .read_config_json(q$study_filter, "questions.study_filter", "[]"),
      from_days_back = q$from_days_back %||% 7,
      delivery = .read_config_json(q$delivery, "questions.delivery", '["file"]'),
      sources = src_obj,
      api_keys = api_keys,
      email_enabled = as.logical(q$email_enabled %||% 0),
      email_cron = q$email_cron %||% "0 7 * * 1",
      email_to = .read_config_json(q$email_to, "questions.email_to", "[]"),
      email_last_sent = q$email_last_sent
    )
  })
}

# One-time seed: import questions.yaml into the DB if empty.
seed_questions_from_yaml <- function(con, yaml_path = "config/questions.yaml") {
  n <- DBI::dbGetQuery(con, "SELECT COUNT(*) n FROM questions")$n
  if (n > 0 || !file.exists(yaml_path)) return(invisible())
  ql <- yaml::read_yaml(yaml_path)$questions
  for (q in ql) {
    DBI::dbExecute(con,
      "INSERT INTO questions(id,label,study_filter,from_days_back,delivery)
       VALUES(?,?,?,?,?)",
      params = list(q$id, q$label,
                    jsonlite::toJSON(q$study_filter %||% list(), auto_unbox = TRUE),
                    q$from_days_back %||% 7,
                    jsonlite::toJSON(q$delivery %||% c("file"), auto_unbox = TRUE)))
    for (k in seq_along(q$concepts)) {
      con_ <- q$concepts[[k]]
      DBI::dbExecute(con,
        "INSERT INTO concepts(question_id,name,position,terms,mesh)
         VALUES(?,?,?,?,?)",
        params = list(q$id, con_$name, k - 1,
                      jsonlite::toJSON(con_$terms %||% list(), auto_unbox = TRUE),
                      jsonlite::toJSON(con_$mesh %||% list(), auto_unbox = TRUE)))
    }
  }
  message("Seeded ", length(ql), " question(s) from YAML into DB.")
}

# Persist new records (full schema) and return only the genuinely new
# ones. Replaces the old `filter_new` write to `seen`.
persist_records <- function(con, recs, question_id, source_priority) {
  if (nrow(recs) == 0) return(recs)
  recs <- .collapse_batch(recs, source_priority)
  # Serialize read-then-insert against ingest and the other scheduled lane.
  DBI::dbExecute(con, "BEGIN IMMEDIATE")
  committed <- FALSE
  on.exit(if (!committed) DBI::dbRollback(con), add = TRUE)
  existing <- DBI::dbGetQuery(con,
    "SELECT * FROM records WHERE question_id = ?", params = list(question_id))
  for (i in which(recs$dedup_key %in% existing$dedup_key)) {
    prior <- existing[match(recs$dedup_key[i], existing$dedup_key), , drop = FALSE]
    fields <- intersect(c("abstract", "authors", "journal", "url", "oa_url"), names(recs))
    filled <- character(0)
    for (field in fields) {
      if ((is.na(prior[[field]][1]) || !nzchar(prior[[field]][1])) &&
          !is.na(recs[[field]][i]) && nzchar(recs[[field]][i])) {
        prior[[field]][1] <- recs[[field]][i]
        filled <- c(filled, field)
      }
    }
    if (length(filled)) DBI::dbExecute(con,
      "UPDATE records SET abstract=?,authors=?,journal=?,url=?,oa_url=?
       WHERE question_id=? AND dedup_key=?",
      params = list(prior$abstract[1], prior$authors[1], prior$journal[1],
                    prior$url[1], prior$oa_url[1], question_id, recs$dedup_key[i]))
    old_prov <- tryCatch(jsonlite::fromJSON(prior$provenance[1] %||% "[]", simplifyVector = FALSE),
      error = function(e) list())
    new_prov <- jsonlite::fromJSON(recs$provenance[i], simplifyVector = FALSE)
    # A source can supply a field to an older persisted record even when it
    # was the primary row of this batch. Record that actual contribution.
    for (field in filled) {
      contributor <- which(vapply(new_prov, function(p) field %in% unlist(p$fields), logical(1)))
      index <- if (length(contributor)) contributor[1L] else 1L
      new_prov[[index]]$fields <- as.list(unique(c(unlist(new_prov[[index]]$fields), field)))
    }
    all_prov <- c(old_prov, new_prov)
    DBI::dbExecute(con, "UPDATE records SET provenance=? WHERE question_id=? AND dedup_key=?",
      params = list(.provenance_json(all_prov),
                    question_id, recs$dedup_key[i]))
  }
  new <- recs[!(recs$dedup_key %in% existing$dedup_key), , drop = FALSE]
  if (nrow(new)) {
    ins <- data.frame(
      dedup_key = new$dedup_key, question_id = question_id,
      source = new$source, ext_id = new$ext_id %||% "", doi = new$doi,
      title = new$title, abstract = new$abstract %||% "",
      authors = new$authors, journal = new$journal,
      pub_date = as.character(new$pub_date), url = new$url,
      oa_url = new$oa_url %||% "", provenance = new$provenance,
      type = new$type, study_type = new$study_type %||% "unclassified",
      is_preprint = new$is_preprint %||% 0L, score = new$score %||% 0,
      stringsAsFactors = FALSE)
    DBI::dbWriteTable(con, "records", ins, append = TRUE)
    channels <- DBI::dbGetQuery(con, "SELECT delivery FROM questions WHERE id=?", params = list(question_id))
    delivery <- if (nrow(channels)) jsonlite::fromJSON(channels$delivery[1] %||% "[]") else character(0)
    for (channel in intersect(delivery, c("zotero", "file"))) {
      for (key in new$dedup_key)
        DBI::dbExecute(con,
          "INSERT OR IGNORE INTO delivery_outbox(channel,question_id,dedup_key) VALUES(?,?,?)",
          params = list(channel, question_id, key))
    }
  }
  DBI::dbCommit(con); committed <- TRUE
  new
}

# ============================================================
# Reclassify records that arrived as 'unclassified' — notably those
# pushed in via the /api/ingest endpoint (external ingestion), which deliberately
# leaves classification to R so the tags-first classifier remains the
# single source of truth. Runs each pipeline cycle; cheap (only touches
# unclassified rows). Re-runs the canonical classifier on title/type.
# ============================================================
# Service-level heartbeat. fetch_log says whether a SOURCE still returns
# anything; this says whether the SERVICE ran at all. A crashed engine writes
# no fetch_log rows either, so without this the two look identical from the
# dashboard. One row per service, overwritten in place.
heartbeat <- function(con, service, ok = TRUE, detail = "", error = "") {
  now <- format(Sys.time(), "%Y-%m-%d %H:%M:%S", tz = "UTC")
  tryCatch({
    DBI::dbExecute(con,
      "INSERT INTO service_heartbeat(service,last_run,last_ok,last_detail,last_error)
       VALUES(?,?,?,?,?)
       ON CONFLICT(service) DO UPDATE SET
         last_run    = excluded.last_run,
         last_ok     = COALESCE(excluded.last_ok, service_heartbeat.last_ok),
         last_detail = excluded.last_detail,
         last_error  = excluded.last_error",
      params = list(service, now, if (isTRUE(ok)) now else NA_character_,
                    detail, error))
  }, error = function(e) invisible(NULL))   # never let telemetry break a run
  invisible(NULL)
}

# Write one fetch_log row per source (health heartbeat; see /api/source_health)
# Accepts either the structured form fetch_question() now produces --
# list(n =, ok =, error =) per source -- or a bare row count for compatibility.
log_fetch <- function(con, question_id, track, source_counts) {
  if (is.null(source_counts) || length(source_counts) == 0) return(invisible(NULL))
  for (src in names(source_counts)) {
    entry <- source_counts[[src]]
    if (is.list(entry)) {
      n  <- entry$n %||% 0
      ok <- isTRUE(entry$ok)
      if (!ok) message("  [WARN] ", track, "/", src, ": ", entry$error %||% "fetch failed")
    } else {
      n  <- entry %||% 0
      ok <- TRUE
    }
    DBI::dbExecute(con,
      "INSERT INTO fetch_log(question_id, track, source, n_fetched, ok) VALUES (?,?,?,?,?)",
      params = list(question_id, track, src, as.integer(n), as.integer(ok)))
  }
  invisible(NULL)
}

# Write Unpaywall OA links found for freshly-persisted records back to the DB.
update_oa_urls <- function(con, recs, question_id) {
  if (is.null(recs) || nrow(recs) == 0 || is.null(recs$oa_url)) return(invisible(0))
  idx <- which(nzchar(recs$oa_url))
  for (i in idx) {
    DBI::dbExecute(con,
      "UPDATE records SET oa_url = ? WHERE dedup_key = ? AND question_id = ?",
      params = list(recs$oa_url[i], recs$dedup_key[i], question_id))
  }
  invisible(length(idx))
}

reclassify_unclassified <- function(con, question_id, q = NULL, settings = NULL) {
  rows <- DBI::dbGetQuery(con,
    "SELECT dedup_key, source, title, abstract, type, journal, pub_date
       FROM records
      WHERE question_id = ? AND (study_type = 'unclassified' OR study_type IS NULL)",
    params = list(question_id))
  if (nrow(rows) == 0) return(invisible(0))
  rows$pub_date <- .parse_date(rows$pub_date)
  # classify_study_type expects a data.frame with these columns
  rows$study_type <- NA_character_
  classified <- classify_study_type(rows)   # from 06_classify.R
  # Rescore too: ingested records arrive with a capped placeholder score;
  # the canonical ranking (relevance/recency/source) is R's job, same as
  # classification. Needs q + settings; skipped when not supplied.
  if (!is.null(q) && !is.null(settings)) {
    if (is.null(classified$pub_date)) classified$pub_date <- as.Date(NA)
    if (is.null(classified$abstract)) classified$abstract <- ""
    rescored <- tryCatch(rank_records(classified, q, settings),
                         error = function(e) classified)
    if (!is.null(rescored$score))
      classified$score <- rescored$score[match(classified$dedup_key, rescored$dedup_key)]
  }
  updated <- 0
  for (i in seq_len(nrow(classified))) {
    st <- classified$study_type[i]
    pp <- classified$is_preprint[i]
    # An unknown study design can still have a confirmed preprint flag and a
    # useful relevance score. Do not leave those values at ingestion defaults.
    if (!is.na(st)) {
      sc <- classified$score[i]
      if (!is.null(sc) && !is.na(sc)) {
        DBI::dbExecute(con,
          "UPDATE records SET study_type = ?, is_preprint = ?, score = ? WHERE dedup_key = ? AND question_id = ?",
          params = list(st, as.integer(pp), as.numeric(sc), classified$dedup_key[i], question_id))
      } else {
        DBI::dbExecute(con,
          "UPDATE records SET study_type = ?, is_preprint = ? WHERE dedup_key = ? AND question_id = ?",
          params = list(st, as.integer(pp), classified$dedup_key[i], question_id))
      }
      updated <- updated + 1
    }
  }
  invisible(updated)
}
