# ============================================================
# 09_signals_persist.R — dedup, rank, and persist signals.
# Signals have no DOI; dedup key = source + normalized ext_id.
# Ranking weights recency + confidence + term-overlap relevance,
# with signal-type priority (a trial result > a grant > a patent
# for "is something happening in therapy").
# ============================================================

.norm_extid <- function(x) {
  if (is.null(x)) return("")
  x <- as.character(x); x[is.na(x)] <- ""
  x <- tolower(trimws(x))
  gsub("[^a-z0-9]", "", x)
}

# Confidence as a numeric weight (authoritative ranks above unverified,
# but unverified is NOT dropped — it's a lead, surfaced lower).
.confidence_weight <- function(conf) {
  switch(conf %||% "reported",
         authoritative = 1.0, reported = 0.6, unverified = 0.3, 0.3)
}

# Signal-type priority for "early therapy intelligence".
.SIGNAL_TYPE_PRIORITY <- c(
  trial_result        = 5,   # results before publication — gold
  press_release       = 4,   # company topline — earliest, if true
  ir_alert            = 4,   # company investor-relations alert — earliest legal signal
  sec_filing          = 4,   # 8-K material disclosure
  regulatory          = 4,   # approval / designation
  aggregator          = 4,   # embargoed-research wire (often same-day as pub)
  literature_alert    = 3,   # journal online-first (ahead of PubMed indexing)
  trial_registration  = 3,   # a trial is starting
  conference          = 3,
  institution         = 3,   # institute/funder press about their own work
  trade_press         = 2,   # pharma industry news (fast on readouts)
  grant               = 2,   # work is beginning
  news                = 1,   # general news — downstream, corroboration
  patent              = 1
)

rank_signals <- function(sig, q, settings) {
  if (nrow(sig) == 0) { sig$score <- numeric(0); return(sig) }
  terms <- tolower(.signal_terms(q))
  txt <- tolower(paste(sig$title, sig$summary))
  rel <- vapply(txt, function(t)
    sum(vapply(terms, function(tm) grepl(tm, t, fixed = TRUE), logical(1))) /
      max(length(terms), 1), numeric(1))

  d <- .parse_date(sig$signal_date)
  sig$signal_date <- as.character(d)
  oldest <- suppressWarnings(min(d, na.rm = TRUE))
  span <- if (!is.finite(as.numeric(oldest))) 1 else max(as.numeric(Sys.Date() - oldest), 1)
  recency <- 1 - (as.numeric(Sys.Date() - d) / span)
  recency[!is.finite(recency)] <- 0
  recency <- pmax(0, pmin(1, recency))

  conf <- vapply(sig$confidence, .confidence_weight, numeric(1))
  tprio <- unname(.SIGNAL_TYPE_PRIORITY[sig$signal_type])
  tprio[is.na(tprio)] <- 1
  tprio <- tprio / 5

  sig$score <- 2.0 * rel + 1.0 * recency + 1.5 * conf + 1.0 * tprio
  sig[order(-sig$score), , drop = FALSE]
}

# Persist new signals; return only genuinely new ones.
persist_signals <- function(con, sig, question_id) {
  if (nrow(sig) == 0) return(sig)
  sig$signal_date <- as.character(.parse_date(sig$signal_date))
  # Stable identifiers are essential: a blank key would collapse unrelated
  # signals from the same source. A URL is an acceptable source identifier.
  missing <- is.na(sig$ext_id) | !nzchar(sig$ext_id)
  sig$ext_id[missing] <- sig$url[missing]
  if (any(is.na(sig$ext_id) | !nzchar(sig$ext_id)))
    stop("A signal has neither an external identifier nor a URL")
  fields <- c("source", "ext_id", "signal_type", "title", "summary", "org",
              "signal_date", "url", "confidence", "extra")
  value <- function(row, field) {
    x <- row[[field]][1]
    if (is.null(x) || is.na(x)) "" else as.character(x)
  }
  canonical_extra <- function(text) {
    sort_objects <- function(x) {
      if (!is.list(x)) return(x)
      if (!is.null(names(x))) x <- x[order(names(x))]
      lapply(x, sort_objects)
    }
    tryCatch(sort_objects(jsonlite::fromJSON(text, simplifyVector = FALSE)), error = function(e) text)
  }
  same <- function(left, right) all(vapply(fields, function(field) {
    if (field == "extra") return(isTRUE(all.equal(canonical_extra(value(left, field)),
      canonical_extra(value(right, field)), tolerance = 0)))
    identical(value(left, field), value(right, field))
  }, logical(1)))
  DBI::dbExecute(con, "BEGIN IMMEDIATE")
  committed <- FALSE
  on.exit(if (!committed) DBI::dbRollback(con), add = TRUE)
  added <- list()
  for (i in seq_len(nrow(sig))) {
    row <- sig[i, , drop = FALSE]
    base <- paste0(row$source, ":", .norm_extid(row$ext_id))
    prefix <- paste0(base, ":revision:")
    prior <- DBI::dbGetQuery(con,
      "SELECT * FROM signals WHERE question_id=? AND (dedup_key=? OR substr(dedup_key,1,?)=?)",
      params = list(question_id, base, nchar(prefix), prefix))
    if (nrow(prior) && any(vapply(seq_len(nrow(prior)),
        function(j) same(row, prior[j, , drop = FALSE]), logical(1)))) next
    key <- base
    revision <- 0L
    while (key %in% prior$dedup_key) {
      revision <- revision + 1L
      key <- paste0(prefix, revision)
    }
    row$dedup_key <- key
    ins <- data.frame(dedup_key = key, question_id = question_id,
      signal_type = row$signal_type, source = row$source, ext_id = row$ext_id,
      title = row$title, summary = row$summary, org = row$org,
      signal_date = row$signal_date, url = row$url, confidence = row$confidence,
      extra = row$extra, score = row$score %||% 0, stringsAsFactors = FALSE)
    DBI::dbWriteTable(con, "signals", ins, append = TRUE)
    added[[length(added) + 1L]] <- row
  }
  DBI::dbCommit(con); committed <- TRUE
  if (!length(added)) return(sig[0, , drop = FALSE])
  do.call(rbind, added)
}

# Load per-question signal config from the DB (toggles, targets, keys).
load_signal_config <- function(con, question_id) {
  row <- DBI::dbGetQuery(con,
    "SELECT * FROM signal_config WHERE question_id = ?",
    params = list(question_id))
  if (nrow(row) == 0) {
    return(list(sources = list(), min_confidence = "unverified",
                scrape_targets = list(), api_keys = list()))
  }
  keys_enc <- .config_map(.read_config_json(row$api_keys_enc, "signal_config.api_keys_enc", "{}"),
    "signal_config.api_keys_enc", kind = "character")
  sources <- .config_map(.read_config_json(row$sources, "signal_config.sources", "{}"),
    "signal_config.sources", allowed = c("sec_edgar", "nih_reporter", "openfda", "trials",
      "patentsview", "epo_ops", "feeds", "cortellis", "citeline"))
  categories <- .config_map(.read_config_json(row$feed_categories, "signal_config.feed_categories", "{}"),
    "signal_config.feed_categories", allowed = c("literature_alert", "institution", "aggregator",
      "trade_press", "news", "ir_alert", "conference"))
  targets <- .read_config_json(row$scrape_targets, "signal_config.scrape_targets", "[]", simplify = FALSE)
  if (!is.list(targets) || (!is.null(names(targets)) && length(targets)) ||
      !all(vapply(targets, is.list, logical(1))))
    stop("Invalid array in signal_config.scrape_targets", call. = FALSE)
  confidence <- row$min_confidence %||% "unverified"
  if (!confidence %in% c("unverified", "reported", "authoritative"))
    stop("Invalid signal_config.min_confidence", call. = FALSE)
  list(
    sources = sources,
    min_confidence = confidence,
    feed_categories = categories,
    scrape_targets = targets,
    api_keys = lapply(keys_enc, .decrypt_key)   # from 07_db_questions.R
  )
}
