# ============================================================
# 02_fetch.R
# Fetch from each source, normalize to a common record schema:
#   source, ext_id, doi, title, abstract, authors, journal,
#   pub_date, url, type
# Every fetcher returns a data.frame with these columns (0 rows ok).
# ============================================================

.empty_records <- function() {
  data.frame(
    source = character(), ext_id = character(), doi = character(),
    title = character(), abstract = character(), authors = character(),
    journal = character(), pub_date = as.Date(character()), url = character(),
    type = character(), stringsAsFactors = FALSE
  )
}

# A failed fetch and an empty fetch are NOT the same thing. Both produce a
# zero-row frame, and if the health log cannot tell them apart then a source
# that has been broken for a month looks exactly like a source that has had a
# quiet month -- the single worst failure mode of a surveillance system. The
# failure is recorded on the frame so fetch_question() can pass it through to
# fetch_log().
.fetch_diagnostic <- function(error, secrets = character(0)) {
  detail <- conditionMessage(error)
  secrets <- unique(as.character(unlist(secrets, use.names = FALSE)))
  for (secret in secrets[!is.na(secrets) & nzchar(secrets)]) {
    detail <- gsub(secret, "[redacted]", detail, fixed = TRUE)
    detail <- gsub(utils::URLencode(secret, reserved = TRUE), "[redacted]", detail, fixed = TRUE)
  }
  detail <- gsub("(?i)([?&](?:api_key|apikey|key|access_token|token)=)[^&[:space:]<>]+",
                 "\\1[redacted]", detail, perl = TRUE)
  detail <- gsub("(?i)(https?://)[^/@[:space:]]+:[^/@[:space:]]+@",
                 "\\1[redacted]@", detail, perl = TRUE)
  detail
}

.safe <- function(expr, label, empty = .empty_records, secrets = character(0)) {
  tryCatch(expr, error = function(e) {
    detail <- .fetch_diagnostic(e, secrets)
    message(sprintf("  [WARN] %s failed: %s", label, detail))
    out <- empty()
    attr(out, "fetch_error") <- detail
    out
  })
}

# Preserve partial results while making an incomplete fetch visible to the
# source-health endpoint. Nested adapters must not turn failures into a quiet
# zero-row success. This same contract is used by the signals track.
.fetch_issue <- function(rows, issue) {
  attr(rows, "fetch_error") <- paste(unique(c(attr(rows, "fetch_error"), issue)), collapse = "; ")
  rows
}

.fetch_bind <- function(parts, empty = .empty_records) {
  if (!length(parts)) return(empty())
  errors <- unlist(lapply(parts, attr, "fetch_error"), use.names = FALSE)
  out <- do.call(rbind, c(parts, list(make.row.names = FALSE)))
  if (is.null(out)) out <- empty()
  if (length(errors)) out <- .fetch_issue(out, paste(unique(errors), collapse = "; "))
  out
}

.fetch_health <- function(rows) list(n = nrow(rows),
  ok = is.null(attr(rows, "fetch_error")), error = attr(rows, "fetch_error") %||% "")

.fetch_limit <- function(settings) {
  value <- settings$fetch$max_records %||% 5000L
  if (length(value) != 1L || !is.numeric(value) || !is.finite(value) ||
      value < 1 || value != floor(value)) stop("fetch.max_records must be a positive integer")
  as.integer(min(value, 100000L))
}

# --- PubMed via rentrez ---------------------------------------
fetch_pubmed <- function(query, days_back, settings) {
  previous_key <- Sys.getenv("ENTREZ_KEY", unset = NA_character_)
  on.exit(if (is.na(previous_key)) Sys.unsetenv("ENTREZ_KEY") else
    Sys.setenv(ENTREZ_KEY = previous_key), add = TRUE)
  key <- settings$ncbi$api_key %||% Sys.getenv("ENTREZ_KEY", "")
  if (nzchar(key)) rentrez::set_entrez_key(key)
  limit <- min(.fetch_limit(settings), 10000L)
  s <- rentrez::entrez_search(
    db = "pubmed", term = query, reldate = days_back,
    datetype = "edat", retmax = 500, use_history = TRUE
  )
  if (s$count == 0) return(.empty_records())
  recs <- list()
  for (start in seq.int(0L, min(s$count, limit) - 1L, by = 500L)) {
    page <- rentrez::entrez_summary(db = "pubmed", web_history = s$web_history,
      retstart = start, retmax = min(500L, limit - start), always_return_list = TRUE)
    recs <- c(recs, page)
  }
  out <- do.call(rbind, lapply(recs, function(r) {
    doi <- ""
    if (!is.null(r$articleids)) {
      d <- r$articleids$value[r$articleids$idtype == "doi"]
      if (length(d)) doi <- d[1]
    }
    # r$pubtype holds the authoritative MeSH publication types. Join with
    # "; " so the classifier can split them back into structured tags.
    data.frame(
      source = "pubmed", ext_id = r$uid, doi = doi,
      title = r$title %||% "", abstract = "",   # esummary lacks abstract
      authors = paste(vapply(r$authors$name, identity, character(1)), collapse = "; "),
      journal = r$source %||% "",
      pub_date = .parse_date(r$sortpubdate %||% r$epubdate %||% r$pubdate),
      url = paste0("https://pubmed.ncbi.nlm.nih.gov/", r$uid, "/"),
      type = paste(unlist(r$pubtype), collapse = "; "),
      stringsAsFactors = FALSE
    )
  }))
  if (s$count > limit) out <- .fetch_issue(out, "PubMed result limit reached; narrow the question or lookback")
  out
}

# --- Europe PMC via europepmc ---------------------------------
# Scalar field out of one raw Europe PMC hit, always length 1.
.epmc_field <- function(h, name, default = "") {
  v <- h[[name]]
  if (is.null(v) || length(v) == 0) return(default)
  as.character(v)[1]
}

fetch_europepmc <- function(query, days_back, settings) {
  from <- format(Sys.Date() - days_back, "%Y-%m-%d")
  q <- sprintf('(%s) AND (FIRST_PDATE:[%s TO %s])', query, from, format(Sys.Date(), "%Y-%m-%d"))
  # output = "raw" is what carries abstractText. The default "parsed" result
  # set omits that column entirely, so every Europe PMC abstract came back
  # empty -- silently, because `hits$abstractText %||% ""` recycles a
  # length-1 "" across the frame. That starved the relevance term in
  # 04_rank.R (which scores title + abstract) and left digests abstract-less
  # for the source ARCHITECTURE.md names as the abstract-bearing one.
  limit <- .fetch_limit(settings)
  hits <- europepmc::epmc_search(query = q, limit = limit, verbose = FALSE,
                                 output = "raw")
  if (is.null(hits) || length(hits) == 0) return(.empty_records())
  out <- do.call(rbind, lapply(hits, function(h) {
    doi <- .epmc_field(h, "doi")
    id  <- .epmc_field(h, "id", .epmc_field(h, "pmid"))
    src <- .epmc_field(h, "source")
    pts <- h$pubTypeList$pubType
    data.frame(
      source = "europepmc", ext_id = id, doi = doi,
      title = .epmc_field(h, "title"),
      abstract = .epmc_field(h, "abstractText"),
      authors = .epmc_field(h, "authorString"),
      journal = .epmc_field(h$journalInfo$journal, "title", src),
      pub_date = .parse_date(.epmc_field(h, "firstPublicationDate",
                                         .epmc_field(h, "pubYear", NA_character_))),
      url = if (nzchar(doi)) paste0("https://doi.org/", doi)
            else paste0("https://europepmc.org/article/", src, "/", id),
      type = if (is.null(pts)) "" else paste(unlist(pts), collapse = "; "),
      stringsAsFactors = FALSE
    )
  }))
  if (length(hits) >= limit) out <- .fetch_issue(out, "Europe PMC result limit reached; completeness is unknown")
  out
}

# --- OpenAlex native API -------------------------------------
# OR within concept blocks, AND between blocks. Native cursor requests keep
# paging limits and failed pages visible; client libraries can return NULL
# for both an empty result and a failed request. The polite pool is retired.
fetch_openalex <- function(concept_terms, days_back, settings) {
  from <- format(Sys.Date() - days_back, "%Y-%m-%d")
  blocks <- vapply(concept_terms, function(terms) {
    terms <- terms[!is.na(terms) & nzchar(terms)]
    if (!length(terms)) return("")
    paste0("(", paste(terms, collapse = " OR "), ")")
  }, character(1))
  blocks <- blocks[nzchar(blocks)]
  if (!length(blocks)) return(.empty_records())
  key <- settings$openalex$api_key %||% Sys.getenv("OPENALEX_API_KEY", "")
  limit <- .fetch_limit(settings)
  works <- list(); cursor <- "*"; seen <- character(0); issue <- NULL
  repeat {
    req <- httr2::request("https://api.openalex.org/works") |>
      httr2::req_url_query(`search.title_and_abstract` = paste(blocks, collapse = " AND "),
        filter = paste0("from_publication_date:", from), cursor = cursor,
        per_page = min(100L, limit - length(works))) |>
      httr2::req_timeout(30) |> httr2::req_retry(max_tries = 3)
    if (nzchar(key)) req <- httr2::req_headers(req, Authorization = paste("Bearer", key))
    js <- tryCatch(httr2::resp_body_json(httr2::req_perform(req)), error = identity)
    if (inherits(js, "error")) {
      if (!length(works)) stop(js)
      issue <- "OpenAlex page request failed; retrieval incomplete"
      break
    }
    page <- js$results %||% list()
    works <- c(works, page)
    next_cursor <- js$meta$next_cursor
    total <- js$meta$count %||% length(works)
    if (!length(page) || length(works) >= total || is.null(next_cursor)) {
      if (length(works) < total) issue <- "OpenAlex ended before its reported total; retrieval incomplete"
      break
    }
    if (!nzchar(key)) {
      issue <- "OpenAlex requires an API key for continued cursor paging; retrieval incomplete"
      break
    }
    if (length(works) >= limit || next_cursor %in% seen || identical(next_cursor, cursor)) {
      issue <- "OpenAlex result limit or repeated cursor; retrieval incomplete"
      break
    }
    seen <- c(seen, cursor); cursor <- next_cursor
  }
  if (!length(works)) return(if (is.null(issue)) .empty_records() else .fetch_issue(.empty_records(), issue))
  out <- do.call(rbind, lapply(works, function(w) {
    doi <- sub("^https?://doi.org/", "", w$doi %||% "")
    data.frame(source = "openalex", ext_id = w$id %||% "", doi = doi,
      title = w$display_name %||% w$title %||% "",
      abstract = .openalex_abstract(w$abstract_inverted_index),
      authors = paste(vapply(w$authorships %||% list(),
        function(a) a$author$display_name %||% "", character(1)), collapse = "; "),
      journal = w$primary_location$source$display_name %||% "",
      pub_date = .parse_date(w$publication_date), url = w$id %||% "",
      type = w$type %||% "", stringsAsFactors = FALSE)
  }))
  if (!is.null(issue)) out <- .fetch_issue(out, issue)
  out
}

.openalex_abstract <- function(index) {
  if (!length(index)) return("")
  positions <- unlist(index, use.names = FALSE)
  words <- rep(names(index), lengths(index))
  keep <- is.finite(positions) & positions >= 0 & positions == floor(positions)
  paste(words[keep][order(positions[keep])], collapse = " ")
}

# openalexR returns a tibble whose column set varies with what the API had
# for those works: `ab`, `author` and `so` are routinely absent. Reading a
# missing column yields NULL, and a NULL/zero-length column makes data.frame()
# throw "arguments imply differing number of rows" -- which .safe() then turns
# into a silent zero-row OpenAlex fetch. Always return a full-length column.
.col <- function(df, name, default = "") {
  v <- df[[name]]
  if (is.null(v) || length(v) == 0) return(rep(default, nrow(df)))
  v[is.na(v)] <- default
  as.character(v)
}

.authors_col <- function(df) {
  a <- df[["author"]]
  if (is.null(a) || length(a) == 0) return(rep("", nrow(df)))
  vapply(a, function(x)
    if (is.data.frame(x) && "au_display_name" %in% names(x))
      paste(x$au_display_name, collapse = "; ") else "",
    character(1))
}

# --- ClinicalTrials.gov v2 (httr2) ----------------------------
.fetch_ctgov_studies <- function(query, days_back, settings) {
  from <- format(Sys.Date() - days_back, "%Y-%m-%d")
  limit <- .fetch_limit(settings)
  studies <- list(); token <- NULL; seen <- character(0)
  repeat {
    req <- httr2::request("https://clinicaltrials.gov/api/v2/studies") |>
      httr2::req_url_query(`query.term` = query,
        `filter.advanced` = sprintf("AREA[LastUpdatePostDate]RANGE[%s,MAX]", from),
        pageSize = min(200L, limit - length(studies)), format = "json") |>
      httr2::req_timeout(30) |> httr2::req_retry(max_tries = 3)
    if (!is.null(token)) req <- httr2::req_url_query(req, pageToken = token)
    js <- tryCatch(httr2::resp_body_json(httr2::req_perform(req)), error = identity)
    if (inherits(js, "error")) {
      if (!length(studies)) stop(js)
      attr(studies, "fetch_error") <- "ClinicalTrials.gov page request failed; retrieval incomplete"
      break
    }
    page <- js$studies %||% list()
    studies <- c(studies, page)
    token <- js$nextPageToken
    if (is.null(token) || !nzchar(token)) break
    if (!length(page) || token %in% seen || length(studies) >= limit) {
      attr(studies, "fetch_error") <- "ClinicalTrials.gov result limit or repeated cursor; retrieval incomplete"
      break
    }
    seen <- c(seen, token)
  }
  studies
}

fetch_clinicaltrials <- function(query, days_back, settings) {
  studies <- .fetch_ctgov_studies(query, days_back, settings)
  if (length(studies) == 0) return(if (is.null(attr(studies, "fetch_error"))) .empty_records()
    else .fetch_issue(.empty_records(), attr(studies, "fetch_error")))
  out <- do.call(rbind, lapply(studies, function(s) {
    p <- s$protocolSection
    nct <- p$identificationModule$nctId %||% ""
    data.frame(
      source = "clinicaltrials", ext_id = nct, doi = "",
      title = p$identificationModule$briefTitle %||% "",
      abstract = p$descriptionModule$briefSummary %||% "",
      authors = p$sponsorCollaboratorsModule$leadSponsor$name %||% "",
      journal = "ClinicalTrials.gov",
      pub_date = .parse_date(p$statusModule$lastUpdatePostDateStruct$date %||%
                               p$statusModule$studyFirstPostDateStruct$date),
      url = paste0("https://clinicaltrials.gov/study/", nct),
      type = paste0("Trial: ", p$designModule$studyType %||% "NA"),
      stringsAsFactors = FALSE
    )
  }))
  if (!is.null(attr(studies, "fetch_error"))) out <- .fetch_issue(out, attr(studies, "fetch_error"))
  out
}

# --- bioRxiv / medRxiv ----------------------------------------
# API returns recent window; we filter client-side via matcher fn.
fetch_biorxiv <- function(matcher_fn, days_back, settings) {
  servers <- c("biorxiv", "medrxiv")
  from <- format(Sys.Date() - days_back, "%Y-%m-%d"); to <- format(Sys.Date(), "%Y-%m-%d")
  out <- .empty_records(); failures <- character(0)
  for (srv in servers) {
    cursor <- 0; more <- TRUE
    while (more) {
      url <- sprintf("https://api.biorxiv.org/details/%s/%s/%s/%d", srv, from, to, cursor)
      js <- tryCatch(jsonlite::fromJSON(url), error = function(e) e)
      if (inherits(js, "error")) {
        failures <- c(failures, paste(srv, conditionMessage(js)))
        break
      }
      coll <- js$collection
      if (is.null(coll) || length(coll) == 0 || !is.data.frame(coll)) break
      keep <- vapply(seq_len(nrow(coll)), function(i)
        matcher_fn(paste(coll$title[i], coll$abstract[i])), logical(1))
      if (any(keep)) {
        c2 <- coll[keep, , drop = FALSE]
        out <- rbind(out, data.frame(
          source = srv, ext_id = c2$doi, doi = c2$doi,
          title = c2$title, abstract = c2$abstract,
          authors = c2$authors, journal = paste0(srv, " (preprint)"),
          pub_date = .parse_date(c2$date), url = paste0("https://doi.org/", c2$doi),
          type = "Preprint", stringsAsFactors = FALSE
        ))
      }
      total <- as.numeric(js$messages$total %||% 0)
      cursor <- cursor + nrow(coll)
      more <- cursor < total
      if (more && cursor >= .fetch_limit(settings)) {
        failures <- c(failures, paste(srv, "page limit reached; retrieval incomplete"))
        break
      }
    }
  }
  if (length(failures)) out <- .fetch_issue(out, paste(failures, collapse = "; "))
  out
}


# --- Semantic Scholar (free API; optional key for higher limits) -
# Per-concept relevance search + client-side intersect, mirroring the
# OpenAlex approach. A free API key (settings$semanticscholar$api_key,
# obtained by registration) only raises the rate limit; works without.
fetch_semanticscholar <- function(concept_terms, days_back, settings) {
  base <- "https://api.semanticscholar.org/graph/v1/paper/search"
  key  <- settings$semanticscholar$api_key %||% ""
  yr_from <- as.integer(format(Sys.Date() - days_back, "%Y"))
  fields <- "title,abstract,year,publicationDate,externalIds,authors,venue,publicationTypes,url"

  issues <- character(0)
  search_one <- function(terms) {
    # Relevance search has no Boolean OR grammar. Search each synonym and
    # union it before intersecting concept blocks, so synonyms are alternatives.
    records <- list()
    for (term in unique(terms)) {
      offset <- 0L; limit <- min(.fetch_limit(settings), 1000L)
      repeat {
        req <- httr2::request(base) |>
          httr2::req_url_query(query = term, fields = fields,
            year = paste0(yr_from, "-"), limit = min(100L, limit - offset), offset = offset) |>
          httr2::req_timeout(30) |> httr2::req_retry(max_tries = 3)
        if (nzchar(key)) req <- httr2::req_headers(req, `x-api-key` = key)
        js <- tryCatch(httr2::resp_body_json(httr2::req_perform(req)), error = identity)
        if (inherits(js, "error")) {
          issues <<- c(issues, "Semantic Scholar request failed; retrieval incomplete")
          break
        }
        page <- js$data %||% list()
        records <- c(records, page)
        offset <- offset + length(page)
        total <- js$total %||% offset
        if (!length(page) || offset >= total || is.null(js$`next`)) {
          if (offset < total) issues <<- c(issues, "Semantic Scholar ended before its reported total; retrieval incomplete")
          break
        }
        if (offset >= limit || js$`next` < offset) {
          issues <<- c(issues, "Semantic Scholar relevance-search limit reached; retrieval incomplete")
          break
        }
        offset <- as.integer(js$`next`)
      }
    }
    ids <- vapply(records, function(p) p$paperId %||% "", character(1))
    records <- stats::setNames(records, ids)
    records[!duplicated(names(records))]
  }

  # The outer .safe records any failed synonym/concept request as a failure.
  cache <- lapply(concept_terms, search_one)
  per_concept <- lapply(cache, function(res) if (is.null(res)) character(0) else names(res))
  common <- Reduce(intersect, per_concept)
  common <- common[nzchar(common)]
  if (length(common) == 0) {
    out <- .empty_records()
    if (length(issues)) out <- .fetch_issue(out, paste(unique(issues), collapse = "; "))
    return(out)
  }

  all_papers <- list()
  for (res in cache) if (!is.null(res)) all_papers <- c(all_papers, res)
  all_papers <- all_papers[!duplicated(names(all_papers))]
  papers <- all_papers[common]

  out <- do.call(rbind, lapply(papers, function(p) {
    if (is.null(p)) return(NULL)
    ext <- p$externalIds %||% list()
    doi <- ext$DOI %||% ""
    pts <- paste(unlist(p$publicationTypes), collapse = "; ")
    data.frame(
      source = "semanticscholar", ext_id = p$paperId %||% "", doi = doi,
      title = p$title %||% "", abstract = p$abstract %||% "",
      authors = paste(vapply(p$authors %||% list(),
                             function(a) a$name %||% "", character(1)), collapse = "; "),
      journal = p$venue %||% "",
      pub_date = .parse_date(p$publicationDate %||% as.character(p$year %||% "")),
      url = p$url %||% (if (nzchar(doi)) paste0("https://doi.org/", doi) else ""),
      type = pts, stringsAsFactors = FALSE
    )
  }))
  # The API's year filter is coarser than the requested lookback.
  out <- out[is.na(out$pub_date) | out$pub_date >= Sys.Date() - days_back, , drop = FALSE]
  if (length(issues)) out <- .fetch_issue(out, paste(unique(issues), collapse = "; "))
  out
}

# --- Crossref (free; email -> "polite pool", faster + more reliable) -
# Catches very recent journal articles, sometimes before PubMed indexes
# them. Plain bibliographic query, filtered on indexing date.
fetch_crossref <- function(query, days_back, settings) {
  if (!nzchar(query)) return(.empty_records())
  from <- format(Sys.Date() - days_back, "%Y-%m-%d")
  email <- settings$contact_email %||% ""
  req <- httr2::request("https://api.crossref.org/works") |>
    httr2::req_url_query(
      query.bibliographic = query,
      filter = paste0("from-index-date:", from, ",type:journal-article"),
      rows = min(1000L, .fetch_limit(settings)), sort = "indexed", order = "desc") |>
    httr2::req_timeout(30) |> httr2::req_retry(max_tries = 3)
  if (nzchar(email))
    req <- httr2::req_headers(req,
      `User-Agent` = sprintf("research-pipeline (mailto:%s)", email))
  items <- list(); cursor <- "*"; seen <- character(0); issue <- NULL
  limit <- .fetch_limit(settings)
  repeat {
    page_req <- httr2::req_url_query(req, cursor = cursor, rows = min(1000L, limit - length(items)))
    js <- tryCatch(httr2::resp_body_json(httr2::req_perform(page_req)), error = identity)
    if (inherits(js, "error")) {
      if (!length(items)) stop(js)
      issue <- "Crossref page request failed; retrieval incomplete"
      break
    }
    page <- js$message$items %||% list()
    items <- c(items, page)
    next_cursor <- js$message$`next-cursor`
    total <- js$message$`total-results` %||% length(items)
    if (!length(page) || length(items) >= total || is.null(next_cursor)) {
      if (length(items) < total) issue <- "Crossref ended before its reported total; retrieval incomplete"
      break
    }
    if (length(items) >= limit || next_cursor %in% seen) {
      issue <- "Crossref result limit or repeated cursor; retrieval incomplete"
      break
    }
    seen <- c(seen, cursor); cursor <- next_cursor
  }
  if (length(items) == 0) return(if (is.null(issue)) .empty_records() else .fetch_issue(.empty_records(), issue))
  out <- do.call(rbind, lapply(items, function(it) {
    title <- paste(unlist(it$title), collapse = " ")
    if (!nzchar(title)) return(NULL)
    auth <- ""
    if (!is.null(it$author))
      auth <- paste(vapply(it$author, function(a)
        trimws(paste(a$given %||% "", a$family %||% "")), character(1)), collapse = "; ")
    dp <- it$published$`date-parts`[[1]] %||% it$indexed$`date-parts`[[1]] %||% NULL
    pub_date <- if (!is.null(dp)) .parse_date(paste(dp, collapse = "-")) else as.Date(NA)
    data.frame(
      source = "crossref", ext_id = it$DOI %||% "", doi = it$DOI %||% "",
      title = title, abstract = it$abstract %||% "",
      authors = auth,
      journal = paste(unlist(it$`container-title`), collapse = " "),
      pub_date = pub_date,
      url = if (nzchar(it$DOI %||% "")) paste0("https://doi.org/", it$DOI) else (it$URL %||% ""),
      type = it$type %||% "journal-article", stringsAsFactors = FALSE
    )
  }))
  if (is.null(out)) out <- .empty_records()
  if (!is.null(issue)) out <- .fetch_issue(out, issue)
  out
}

# --- Unpaywall enrichment (free; email param required) ---------
# NOT a fetcher — ENRICHES already-collected records that have a DOI
# with a legal open-access full-text URL. The "find AND read" piece:
# the pipeline finds a paper exists; Unpaywall says where to read it
# free, if an OA copy exists. Adds an `oa_url` column (empty if none).
enrich_unpaywall <- function(recs, settings) {
  if (nrow(recs) == 0) { recs$oa_url <- character(0); return(recs) }
  email <- settings$unpaywall$email %||% settings$contact_email %||% ""
  if (is.null(recs$oa_url)) recs$oa_url <- ""
  if (!nzchar(email)) return(recs)
  idx <- which(nzchar(recs$doi))
  for (i in idx) {
    doi <- recs$doi[i]
    url <- sprintf("https://api.unpaywall.org/v2/%s?email=%s",
                   utils::URLencode(doi, reserved = TRUE),
                   utils::URLencode(email, reserved = TRUE))
    res <- .safe2({
      resp <- httr2::request(url) |>
        httr2::req_retry(max_tries = 2) |>
        httr2::req_perform()
      httr2::resp_body_json(resp)
    })
    if (!is.null(res) && isTRUE(res$is_oa)) {
      loc <- res$best_oa_location %||% list()
      recs$oa_url[i] <- loc$url_for_pdf %||% loc$url %||% ""
    }
  }
  recs
}

# small helper: run expr, return NULL on any error (quietly)
.safe2 <- function(expr) tryCatch(expr, error = function(e) NULL)

# --- orchestrate one question across its selected sources -----
# Per-question `sources` (named logical) override global settings;
# per-question `api_keys` (named, decrypted) override global keys.
fetch_question <- function(q, queries, settings) {
  days <- q$from_days_back %||% 7

  # resolve which sources to hit: question-level toggle if present,
  # else fall back to the global settings$sources default.
  src_on <- function(name) {
    qs <- q$sources[[name]]
    if (!is.null(qs)) isTRUE(qs) else isTRUE(settings$sources[[name]])
  }
  # per-question key for a source, else global
  key_for <- function(name, global) {
    k <- q$api_keys[[name]]
    if (!is.null(k) && nzchar(k)) k else global
  }

  # clone settings with question-specific NCBI key if provided
  s_local <- settings
  s_local$ncbi$api_key <- key_for("pubmed", settings$ncbi$api_key %||% "")
  s_local$semanticscholar$api_key <- key_for("semanticscholar", settings$semanticscholar$api_key %||% "")
  s_local$openalex$api_key <- key_for("openalex", settings$openalex$api_key %||% Sys.getenv("OPENALEX_API_KEY", ""))
  secrets <- c(unlist(q$api_keys, use.names = FALSE), s_local$ncbi$api_key,
               s_local$semanticscholar$api_key, s_local$openalex$api_key,
               Sys.getenv("ENTREZ_KEY", ""))

  recs <- list()
  if (src_on("pubmed"))
    recs$pubmed <- .safe(fetch_pubmed(queries$pubmed, days, s_local), "pubmed", secrets = secrets)
  if (src_on("europepmc"))
    recs$epmc <- .safe(fetch_europepmc(queries$europepmc, days, s_local), "europepmc", secrets = secrets)
  if (src_on("openalex"))
    recs$oa <- .safe(fetch_openalex(queries$openalex, days, s_local), "openalex", secrets = secrets)
  if (src_on("semanticscholar"))
    recs$s2 <- .safe(fetch_semanticscholar(queries$semanticscholar, days, s_local), "semanticscholar", secrets = secrets)
  if (src_on("crossref"))
    recs$cr <- .safe(fetch_crossref(queries$crossref, days, s_local), "crossref", secrets = secrets)
  if (src_on("clinicaltrials"))
    recs$ct <- .safe(fetch_clinicaltrials(queries$clinicaltrials, days, s_local), "clinicaltrials", secrets = secrets)
  if (src_on("biorxiv"))
    recs$bio <- .safe(fetch_biorxiv(queries$biorxiv_match, days, s_local), "biorxiv", secrets = secrets)

  out <- .fetch_bind(recs)
  # Per-source health. Each entry carries the row count AND whether the fetch
  # actually succeeded, so fetch_log can record ok=0 for a broken source
  # instead of logging it as a successful fetch that happened to find nothing.
  attr(out, "source_counts") <- lapply(recs, .fetch_health)
  out
}
