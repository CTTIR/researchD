# ============================================================
# 05_deliver.R
# Three channels: (1) HTML+MD file, (2) email, (3) Zotero push.
# Each respects the question's `delivery` list and global enables.
# ============================================================

# --- build the digest body (HTML), grouped by study type ------
.digest_records <- function(recs) {
  if (!nrow(recs)) return(recs)
  if (is.null(recs$study_type)) recs$study_type <- rep("unclassified", nrow(recs))
  missing <- is.na(recs$study_type) | !nzchar(recs$study_type)
  recs$study_type[missing] <- "unclassified"
  recs
}

render_digest_html <- function(per_question, run_date) {
  parts <- c(sprintf("<h1>Research digest &mdash; %s</h1>", run_date))
  for (qd in per_question) {
    qd$recs <- .digest_records(qd$recs)
    parts <- c(parts, sprintf("<h2>%s <small>(%d new)</small></h2>",
                              htmltools::htmlEscape(qd$label), nrow(qd$recs)))
    if (nrow(qd$recs) == 0) {
      parts <- c(parts, "<p><em>No new records this run.</em></p>"); next
    }
    # group into the four study-type sections, in fixed order
    for (stype in unique(c(STUDY_TYPE_ORDER, qd$recs$study_type))) {
      sub <- qd$recs[qd$recs$study_type == stype, , drop = FALSE]
      if (nrow(sub) == 0) next
      parts <- c(parts, sprintf("<h3 style='border-bottom:1px solid #ddd'>%s <small>(%d)</small></h3>",
                                htmltools::htmlEscape(if (stype %in% names(STUDY_TYPE_LABEL)) STUDY_TYPE_LABEL[[stype]] else stype), nrow(sub)))
      rows <- vapply(seq_len(nrow(sub)), function(i) {
        r <- sub[i, ]
        badge <- sprintf("<span style='background:#eee;border-radius:3px;padding:1px 5px;font-size:11px'>%s</span>", htmltools::htmlEscape(r$source))
        pp <- if (isTRUE(r$is_preprint == 1))
          " <span style='background:#fff3cd;border-radius:3px;padding:1px 5px;font-size:11px'>preprint</span>" else ""
        sprintf(
          "<li style='margin-bottom:10px'>%s%s <a href='%s'><strong>%s</strong></a><br>
           <span style='color:#555;font-size:13px'>%s &middot; %s &middot; score %.2f</span></li>",
          badge, pp, htmltools::htmlEscape(.delivery_url(r$url), attribute = TRUE), htmltools::htmlEscape(r$title),
          htmltools::htmlEscape(substr(r$authors, 1, 120)), htmltools::htmlEscape(as.character(r$pub_date)), r$score)
      }, character(1))
      parts <- c(parts, "<ol>", rows, "</ol>")
    }
  }
  paste(parts, collapse = "\n")
}

render_digest_md <- function(per_question, run_date) {
  parts <- c(sprintf("# Research digest — %s\n", run_date))
  for (qd in per_question) {
    qd$recs <- .digest_records(qd$recs)
    parts <- c(parts, sprintf("## %s (%d new)\n", .md_text(qd$label), nrow(qd$recs)))
    if (nrow(qd$recs) == 0) { parts <- c(parts, "_No new records this run._\n"); next }
    for (stype in unique(c(STUDY_TYPE_ORDER, qd$recs$study_type))) {
      sub <- qd$recs[qd$recs$study_type == stype, , drop = FALSE]
      if (nrow(sub) == 0) next
      label <- if (stype %in% names(STUDY_TYPE_LABEL)) STUDY_TYPE_LABEL[[stype]] else stype
      parts <- c(parts, sprintf("### %s (%d)\n", .md_text(label), nrow(sub)))
      for (i in seq_len(nrow(sub))) {
        r <- sub[i, ]
        pp <- if (isTRUE(r$is_preprint == 1)) " _(preprint)_" else ""
        parts <- c(parts, sprintf("%d. **[%s]**%s [%s](%s)  \n   %s · %s · score %.2f\n",
                                  i, .md_text(r$source), pp, .md_text(r$title), .md_url(r$url),
                                  .md_text(substr(r$authors, 1, 120)), .md_text(as.character(r$pub_date)), r$score))
      }
    }
  }
  paste(parts, collapse = "\n")
}

# --- channel 1: write files -----------------------------------
deliver_file <- function(html, md, settings, run_date) {
  dir.create(settings$paths$digests, showWarnings = FALSE, recursive = TRUE)
  base <- tempfile(pattern = paste0("digest_", run_date, "_"), tmpdir = settings$paths$digests)
  html_tmp <- paste0(base, ".html.tmp"); md_tmp <- paste0(base, ".md.tmp")
  on.exit(unlink(c(html_tmp, md_tmp)), add = TRUE)
  writeLines(html, html_tmp)
  writeLines(md, md_tmp)
  if (!file.rename(html_tmp, paste0(base, ".html")) ||
      !file.rename(md_tmp, paste0(base, ".md"))) stop("Could not finish digest files")
  message("  digest written: ", base, ".{html,md}")
  paste0(base, ".html")
}

# Export every pending file record, including retries and ingested records.
# Mark only the rows actually written; a failure leaves them pending.
deliver_pending_files <- function(con, questions, settings, run_date) {
  batches <- list()
  for (q in questions) {
    if (!"file" %in% q$delivery) next
    recs <- DBI::dbGetQuery(con,
      "SELECT r.* FROM delivery_outbox d JOIN records r
       ON r.question_id=d.question_id AND r.dedup_key=d.dedup_key
       WHERE d.channel='file' AND d.state='pending' AND d.question_id=?
       ORDER BY r.score DESC,r.dedup_key", params = list(q$id))
    if (nrow(recs)) batches[[q$id]] <- list(label=q$label,recs=recs)
  }
  if (!length(batches)) return(invisible(NULL))
  output <- deliver_file(render_digest_html(batches,run_date),
                         render_digest_md(batches,run_date),settings,run_date)
  DBI::dbWithTransaction(con, {
    for (id in names(batches)) for (key in batches[[id]]$recs$dedup_key) {
      DBI::dbExecute(con,
        "UPDATE delivery_outbox SET state='sent',remote_key=?,last_error='',
         attempts=attempts+1,updated_at=datetime('now') WHERE channel='file'
         AND question_id=? AND dedup_key=?", params=list(output,id,key))
    }
  })
  invisible(output)
}

# --- channel 2: email via blastula ----------------------------
deliver_email <- function(html, settings, run_date) {
  if (!isTRUE(settings$email$enabled)) return(invisible())
  pw <- Sys.getenv(settings$email$smtp_pw_env)
  if (!nzchar(pw)) { message("  [skip email] no SMTP password in env"); return(invisible()) }
  msg <- blastula::compose_email(body = blastula::md(html))
  creds <- blastula::creds_envvar(
    user = settings$email$smtp_user,
    pass_envvar = settings$email$smtp_pw_env,
    host = settings$email$smtp_host,
    port = settings$email$smtp_port,
    use_ssl = isTRUE(settings$email$use_ssl)
  )
  blastula::smtp_send(
    msg, from = settings$email$from, to = settings$email$to,
    subject = paste("Research digest —", run_date), credentials = creds
  )
  message("  email sent to ", settings$email$to)
}

# Only web links can be emitted into generated HTML or remote reference items.
.delivery_url <- function(x) {
  x <- x %||% ""
  if (grepl("^https?://", x, ignore.case = TRUE)) x else ""
}

.md_text <- function(x) {
  chars <- strsplit(as.character(x %||% ""), "", fixed=TRUE)[[1]]
  special <- chars %in% c("\\", "`", "*", "_", "{", "}", "[", "]", "(", ")", "#", "+", "-", ".", "!", "|")
  chars[special] <- paste0("\\", chars[special])
  htmltools::htmlEscape(paste(chars, collapse=""))
}

.md_url <- function(x) {
  value <- utils::URLencode(.delivery_url(x), reserved=FALSE, repeated=TRUE)
  value <- gsub("(", "%28", value, fixed=TRUE)
  gsub(")", "%29", value, fixed=TRUE)
}

# --- channel 3: Zotero push (Web API v3) ----------------------
# Creates items per record. Uses DOI when present so Zotero can enrich.
deliver_zotero <- function(recs, question_id, settings, con = NULL) {
  durable <- !is.null(con)
  if (durable) {
    recs <- DBI::dbGetQuery(con,
      "SELECT r.*, d.remote_key, d.destination, d.attempts FROM delivery_outbox d
       JOIN records r ON r.question_id=d.question_id AND r.dedup_key=d.dedup_key
       WHERE d.channel='zotero' AND d.question_id=? AND d.state='pending'
       ORDER BY r.first_seen, r.dedup_key", params = list(question_id))
  }
  if (!nrow(recs)) return(invisible(TRUE))
  if (!isTRUE(settings$zotero$enabled)) return(invisible(FALSE))
  key <- Sys.getenv(settings$zotero$api_key_env)
  lib <- settings$zotero$library_id %||% ""
  if (!nzchar(key) || !nzchar(lib)) stop("Zotero delivery pending: configure key and library_id")
  if (!settings$zotero$library_type %in% c("user", "group") || !grepl("^[0-9]+$", lib))
    stop("Zotero library_type must be user/group and library_id must be numeric")
  base <- sprintf("https://api.zotero.org/%ss/%s", settings$zotero$library_type, lib)
  coll_key <- NULL
  if (isTRUE(settings$zotero$collection_per_question))
    coll_key <- .zotero_ensure_collection(base, key, question_id)

  failed <- character()
  for (i in seq_len(nrow(recs))) {
    r <- recs[i, , drop = FALSE]
    remote <- if (durable) r$remote_key %||% "" else ""
    tryCatch({
      if (durable && nzchar(r$destination %||% "") && r$destination != base)
        stop("Queued destination differs from settings; review before moving pending items")
      if (!nzchar(remote)) {
        remote <- paste(sample(strsplit("23456789ABCDEFGHIJKLMNPQRSTUVWXYZ", "")[[1]],
                               8, replace = TRUE), collapse = "")
        if (durable) DBI::dbExecute(con,
          "UPDATE delivery_outbox SET remote_key=?,destination=?,updated_at=datetime('now')
           WHERE channel='zotero' AND question_id=? AND dedup_key=?",
          params = list(remote, base, question_id, r$dedup_key))
      }
      if (durable) DBI::dbExecute(con,
        "UPDATE delivery_outbox SET attempts=attempts+1 WHERE channel='zotero'
         AND question_id=? AND dedup_key=?", params = list(question_id, r$dedup_key))
      # Reconcile an uncertain prior response using the stable remote item key.
      # This avoids duplicating an item if the server accepted it before a local
      # process/network failure prevented acknowledgement.
      already <- FALSE
      if (durable && r$attempts > 0) {
        check <- httr2::request(paste0(base, "/items/", remote)) |>
          httr2::req_headers(`Zotero-API-Key` = key, `Zotero-API-Version` = "3") |>
          httr2::req_timeout(30) |>
          httr2::req_error(is_error = function(resp) FALSE) |>
          httr2::req_perform()
        status <- httr2::resp_status(check)
        if (status == 200L) {
          saved <- httr2::resp_body_json(check)$data
          if (!identical(saved$title, r$title) ||
              !identical(.norm_doi(saved$DOI), .norm_doi(r$doi)))
            stop("Remote item identity differs from queued record; manual review required")
          already <- TRUE
        } else if (status != 404L) stop("Unable to reconcile pending Zotero item (HTTP ", status, ")")
      }
      if (!already) {
        it <- list(key = remote,
          itemType = if (isTRUE(r$is_preprint == 1L)) "preprint" else "journalArticle",
          title = r$title, DOI = .norm_doi(r$doi), url = .delivery_url(r$url),
          abstractNote = r$abstract %||% "", date = as.character(r$pub_date %||% ""),
          tags = list(), collections = list(), relations = structure(list(), names=character()),
          extra = sprintf("source: %s; question: %s", r$source, question_id))
        if (it$itemType == "journalArticle") it$publicationTitle <- r$journal %||% ""
        authors <- trimws(strsplit(r$authors %||% "", ";", fixed = TRUE)[[1]])
        it$creators <- lapply(authors[nzchar(authors)], function(name)
          list(creatorType = "author", name = name))
        if (!is.null(coll_key)) it$collections <- list(coll_key)
        response <- httr2::request(paste0(base, "/items")) |>
          httr2::req_headers(`Zotero-API-Key` = key, `Zotero-API-Version` = "3") |>
          httr2::req_timeout(30) |>
          httr2::req_body_json(list(it)) |>
          httr2::req_perform() |>
          httr2::resp_body_json()
        rejected <- response$failed %||% list()
        accepted <- response$successful %||% response$success %||% list()
        unchanged <- response$unchanged %||% list()
        if (length(rejected) || (!length(accepted) && !length(unchanged)))
          stop("Zotero did not accept the item; it remains pending")
      }
      if (durable) DBI::dbExecute(con,
        "UPDATE delivery_outbox SET state='sent',last_error='',updated_at=datetime('now')
         WHERE channel='zotero' AND question_id=? AND dedup_key=?",
        params = list(question_id, r$dedup_key))
    }, error = function(e) {
      # Do not persist request objects or credentials in diagnostics.
      detail <- gsub(key, "[redacted]", conditionMessage(e), fixed = TRUE)
      failed <<- c(failed, detail)
      if (durable) DBI::dbExecute(con,
        "UPDATE delivery_outbox SET last_error=?,updated_at=datetime('now')
         WHERE channel='zotero' AND question_id=? AND dedup_key=?",
        params = list(substr(detail, 1, 500), question_id, r$dedup_key))
    })
  }
  if (length(failed)) stop(length(failed), " Zotero item(s) remain pending: ", failed[[1]])
  message("  delivered ", nrow(recs), " items to Zotero")
  invisible(TRUE)
}

.zotero_ensure_collection <- function(base, key, name) {
  start <- 0L
  repeat {
    existing <- httr2::request(paste0(base, "/collections")) |>
      httr2::req_headers(`Zotero-API-Key` = key, `Zotero-API-Version` = "3") |>
      httr2::req_url_query(limit = 100, start = start) |>
      httr2::req_timeout(30) |>
      httr2::req_perform() |> httr2::resp_body_json()
    for (collection in existing)
      if (isTRUE(collection$data$name == name)) return(collection$key)
    if (length(existing) < 100L) break
    start <- start + length(existing)
    if (start >= 10000L) stop("Zotero collection scan limit reached; select a library with fewer collections")
  }
  resp <- httr2::request(paste0(base, "/collections")) |>
    httr2::req_headers(`Zotero-API-Key` = key, `Zotero-API-Version` = "3") |>
    httr2::req_timeout(30) |>
    httr2::req_body_json(list(list(name = name))) |>
    httr2::req_perform() |> httr2::resp_body_json()
  if (length(resp$failed %||% list())) stop("Zotero rejected the collection")
  result <- resp$successful[[1]]$key %||% resp$success[[1]] %||% NULL
  if (is.null(result) || !nzchar(result)) stop("Zotero did not confirm a collection key")
  result
}
