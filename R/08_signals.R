# ============================================================
# 08_signals.R — the SIGNALS track (parallel to the literature track).
#
# Fetches non-paper intelligence and normalizes every source into one
# signals schema:
#   signal_type, source, ext_id, title, summary, org, signal_date,
#   url, confidence, extra (JSON), score
#
# Confidence tiers (speed-vs-reliability, explicit per signal):
#   authoritative : registries, regulators, SEC  (trust the content)
#   reported      : official-but-preliminary (topline results)
#   unverified    : scraped press/IR             (treat as a lead)
#
# Each fetcher is wrapped by the caller in .safe() so one failing
# source never sinks the run.
# ============================================================

.empty_signals <- function() {
  data.frame(
    signal_type = character(), source = character(), ext_id = character(),
    title = character(), summary = character(), org = character(),
    signal_date = as.character(character()), url = character(),
    confidence = character(), extra = character(), score = numeric(),
    stringsAsFactors = FALSE
  )
}

# Build an OR query string from a question's concepts (flat — most of
# these APIs don't support nested boolean well). We OR every term across
# all concepts; relevance scoring later handles precision.
.signal_terms <- function(q) {
  terms <- unique(unlist(lapply(q$concepts, function(c) c(c$terms, c$mesh))))
  terms[!is.na(terms) & nzchar(trimws(terms))]
}
.quote_phrase <- function(x) ifelse(grepl("\\s", x), paste0('"', x, '"'), x)

# ============================================================
# TIER 1 — clean free APIs
# ============================================================

# --- SEC EDGAR full-text search (8-K disclosures, same-day) ----
# Verified endpoint: https://efts.sec.gov/LATEST/search-index
# Free, no key, requires a User-Agent. 8-K = material event (topline
# trial results for US-listed biotech often appear here first).
fetch_sec_edgar <- function(q, days_back, settings) {
  terms <- .signal_terms(q)
  if (length(terms) == 0) return(.empty_signals())
  # EFTS q supports phrase search; OR a handful of the most specific terms.
  # Keep the query focused: too many ORs returns noise.
  qstr <- paste(.quote_phrase(utils::head(terms, 8)), collapse = " OR ")
  from <- format(Sys.Date() - days_back, "%Y-%m-%d")
  to   <- format(Sys.Date(), "%Y-%m-%d")
  ua <- settings$contact_email %||% "research-pipeline (contact@example.com)"

  resp <- httr2::request("https://efts.sec.gov/LATEST/search-index") |>
    httr2::req_url_query(q = qstr, forms = "8-K", dateRange = "custom",
                         startdt = from, enddt = to) |>
    httr2::req_headers(`User-Agent` = ua, Accept = "application/json") |>
    httr2::req_timeout(30) |>
    httr2::req_retry(max_tries = 3) |>
    httr2::req_perform()
  js <- httr2::resp_body_json(resp)
  hits <- js$hits$hits
  if (is.null(hits) || length(hits) == 0) return(.empty_signals())
  page_count <- length(hits)
  hits <- utils::head(hits, .fetch_limit(settings))

  out <- do.call(rbind, lapply(hits, function(h) {
    src <- h$`_source`
    cik <- if (length(src$cik)) src$cik[[1]] else ""
    adsh <- src$`_id` %||% h$`_id` %||% ""
    # build the canonical filing URL
    accno <- gsub("[^0-9]", "", strsplit(adsh, ":")[[1]][1])
    url <- if (nzchar(cik) && nzchar(accno))
      sprintf("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=%s&type=8-K", cik)
      else "https://www.sec.gov/cgi-bin/browse-edgar"
    data.frame(
      signal_type = "sec_filing", source = "sec_edgar",
      ext_id = adsh,
      title = paste0("8-K: ", paste(unlist(src$display_names), collapse = "; ")),
      summary = "", org = paste(unlist(src$display_names), collapse = "; "),
      signal_date = src$file_date %||% "", url = url,
      confidence = "authoritative",
      extra = jsonlite::toJSON(list(form = src$root_forms %||% "8-K",
                                    cik = cik), auto_unbox = TRUE),
      score = 0, stringsAsFactors = FALSE
    )
  }))
  total <- js$hits$total
  if (is.list(total)) total <- total$value
  if ((length(total) && total > nrow(out)) || (!length(total) && page_count >= 100L))
    out <- .fetch_issue(out, "SEC first-page result limit reached; retrieval incomplete")
  out
}

# --- NIH RePORTER (grant awards — earliest possible signal) ----
# POST API. Free, no key. Docs: api.reporter.nih.gov
fetch_nih_reporter <- function(q, days_back, settings) {
  terms <- .signal_terms(q)
  if (length(terms) == 0) return(.empty_signals())
  from <- format(Sys.Date() - days_back, "%Y-%m-%d")
  body <- list(
    criteria = list(
      advanced_text_search = list(
        operator = "or",
        search_field = "projecttitle,terms,abstracttext",
        search_text = paste(terms, collapse = " ")
      ),
      award_notice_date = list(from_date = from,
                               to_date = format(Sys.Date(), "%Y-%m-%d"))
    ),
    include_fields = list("ApplId","ProjectTitle","AbstractText","Organization",
                          "AwardNoticeDate","ProjectNum","ContactPiName",
                          "AgencyIcAdmin","AwardAmount"),
    limit = min(100L, .fetch_limit(settings)), offset = 0
  )
  resp <- httr2::request("https://api.reporter.nih.gov/v2/projects/search") |>
    httr2::req_body_json(body) |>
    httr2::req_timeout(30) |>
    httr2::req_retry(max_tries = 3) |>
    httr2::req_perform()
  js <- httr2::resp_body_json(resp)
  results <- js$results
  if (is.null(results) || length(results) == 0) return(.empty_signals())
  out <- do.call(rbind, lapply(results, function(r) {
    org <- r$organization$org_name %||% ""
    data.frame(
      signal_type = "grant", source = "nih_reporter",
      ext_id = r$project_num %||% "",
      title = r$project_title %||% "",
      summary = substr(r$abstract_text %||% "", 1, 2000),
      org = org, signal_date = r$award_notice_date %||% "",
      url = r$project_detail_url %||% if (!is.null(r$appl_id))
        paste0("https://reporter.nih.gov/project-details/", r$appl_id) else "https://reporter.nih.gov/",
      confidence = "authoritative",
      extra = jsonlite::toJSON(list(
        pi = r$contact_pi_name %||% "",
        amount = r$award_amount %||% NA,
        agency = r$agency_ic_admin$abbreviation %||% ""), auto_unbox = TRUE),
      score = 0, stringsAsFactors = FALSE
    )
  }))
  total <- js$meta$total
  if ((length(total) && total > nrow(out)) || (!length(total) && nrow(out) >= body$limit))
    out <- .fetch_issue(out, "NIH RePORTER first-page result limit reached; retrieval incomplete")
  out
}

# --- openFDA (approvals, designations, safety) -----------------
# Free; optional key raises rate limit. Drug approvals + labels.
fetch_openfda <- function(q, days_back, settings) {
  terms <- .signal_terms(q)
  if (length(terms) == 0) return(.empty_signals())
  # Search drug labels updated recently mentioning the terms.
  search_expr <- paste0("(",
    paste(sprintf('indications_and_usage:"%s"', terms),
          collapse = " OR "), ")")
  from <- format(Sys.Date() - days_back, "%Y%m%d")
  to <- format(Sys.Date(), "%Y%m%d")
  search_expr <- paste0(search_expr, " AND effective_time:[", from, " TO ", to, "]")
  key <- settings$signal_keys$openfda %||% ""
  url <- "https://api.fda.gov/drug/label.json"
  req <- httr2::request(url) |>
    httr2::req_url_query(search = search_expr, limit = min(100L, .fetch_limit(settings))) |>
    httr2::req_timeout(30) |>
    httr2::req_error(is_error = function(resp) httr2::resp_status(resp) >= 400L && httr2::resp_status(resp) != 404L)
  if (nzchar(key)) req <- httr2::req_url_query(req, api_key = key)
  results <- list(); issue <- NULL; limit <- min(.fetch_limit(settings), 25000L)
  repeat {
    page_req <- httr2::req_url_query(req, skip = length(results), limit = min(100L, limit - length(results)))
    resp <- tryCatch(httr2::req_perform(httr2::req_retry(page_req, max_tries = 2)), error = identity)
    if (inherits(resp, "error")) {
      if (!length(results)) stop(resp)
      issue <- "openFDA page request failed; retrieval incomplete"
      break
    }
    if (httr2::resp_status(resp) == 404L) {
      if (length(results)) issue <- "openFDA ended before its reported total; retrieval incomplete"
      break
    }
    js <- tryCatch(httr2::resp_body_json(resp), error = identity)
    if (inherits(js, "error")) {
      if (!length(results)) stop(js)
      issue <- "openFDA page response could not be parsed; retrieval incomplete"
      break
    }
    page <- js$results %||% list()
    results <- c(results, page)
    total <- js$meta$results$total %||% length(results)
    if (!length(page) || length(results) >= total) {
      if (length(results) < total) issue <- "openFDA ended before its reported total; retrieval incomplete"
      break
    }
    if (length(results) >= limit) {
      issue <- "openFDA result limit reached; retrieval incomplete"
      break
    }
  }
  if (length(results) == 0) return(if (is.null(issue)) .empty_signals() else .fetch_issue(.empty_signals(), issue))
  out <- do.call(rbind, lapply(results, function(r) {
    openfda <- r$openfda %||% list()
    brand <- paste(unlist(openfda$brand_name), collapse = "; ")
    data.frame(
      signal_type = "regulatory", source = "openfda",
      ext_id = r$set_id %||% r$id %||% stop("FDA label has no document identifier"),
      title = paste0("FDA label: ", brand),
      summary = substr(paste(unlist(r$indications_and_usage), collapse = " "), 1, 1500),
      org = paste(unlist(openfda$manufacturer_name), collapse = "; "),
      signal_date = as.character(.parse_date(r$effective_time %||% "")),
      url = "https://labels.fda.gov/",
      confidence = "authoritative",
      extra = jsonlite::toJSON(list(brand = brand,
        generic = paste(unlist(openfda$generic_name), collapse = "; ")),
        auto_unbox = TRUE),
      score = 0, stringsAsFactors = FALSE
    )
  }))
  if (!is.null(issue)) out <- .fetch_issue(out, issue)
  out
}

# --- Trial registrations and posted results -------------------
# ClinicalTrials.gov supplies event dates and results-posting metadata through
# its native API. CTIS/ISRCTN remain registry leads imported with ctrdata; an
# import timestamp is never presented as a registration or results date.
.ctgov_signals <- function(studies) {
  if (!length(studies)) return(if (is.null(attr(studies, "fetch_error"))) .empty_signals()
    else .fetch_issue(.empty_signals(), attr(studies, "fetch_error")))
  out <- lapply(studies, function(study) {
    p <- study$protocolSection
    id <- p$identificationModule$nctId %||% ""
    if (!nzchar(id)) stop("ClinicalTrials.gov study has no NCT identifier")
    status <- p$statusModule
    title <- p$identificationModule$briefTitle %||% id
    date <- as.character(.parse_date(status$studyFirstPostDateStruct$date))
    extra <- as.character(jsonlite::toJSON(list(nct_id = id,
      overall_status = status$overallStatus %||% "",
      last_update = status$lastUpdatePostDateStruct$date %||% ""), auto_unbox = TRUE))
    row <- data.frame(signal_type = "trial_registration", source = "ctgov2",
      ext_id = id, title = title,
      summary = p$descriptionModule$briefSummary %||% "",
      org = p$sponsorCollaboratorsModule$leadSponsor$name %||% "",
      signal_date = date, url = paste0("https://clinicaltrials.gov/study/", id),
      confidence = "authoritative", extra = extra, score = 0, stringsAsFactors = FALSE)
    result_date <- status$resultsFirstPostDateStruct$date %||% ""
    if (isTRUE(study$hasResults) || length(study$resultsSection) || nzchar(result_date)) {
      result <- row
      result$signal_type <- "trial_result"
      result$ext_id <- paste0(id, ":results")
      result$title <- paste0("Results posted: ", title)
      result$summary <- "Summary results are available in the trial registry; review the source for outcomes and limitations."
      result$signal_date <- as.character(.parse_date(result_date))
      row <- rbind(row, result)
    }
    row
  })
  result <- do.call(rbind, out)
  if (!is.null(attr(studies, "fetch_error"))) result <- .fetch_issue(result, attr(studies, "fetch_error"))
  result
}

fetch_trials_ctrdata <- function(q, days_back, settings) {
  native <- .safe(.ctgov_signals(.fetch_ctgov_studies(build_clinicaltrials(q), days_back, settings)),
                  "ClinicalTrials.gov signals", empty = .empty_signals)
  if (!requireNamespace("ctrdata", quietly = TRUE))
    return(.fetch_issue(native, "ctrdata is required for CTIS and ISRCTN registry leads"))
  terms <- .signal_terms(q)
  if (!length(terms)) return(native)
  other <- .safe(.fetch_other_trial_registries(terms, days_back),
                 "CTIS/ISRCTN registry leads", empty = .empty_signals)
  .fetch_bind(list(native, other), empty = .empty_signals)
}

.fetch_other_trial_registries <- function(terms, days_back) {
  query <- paste(terms, collapse = " OR ")
  tmp <- tempfile(fileext = ".sqlite")
  con <- nodbi::src_sqlite(dbname = tmp, collection = "trials")
  on.exit({ try(DBI::dbDisconnect(con$con), silent = TRUE); unlink(tmp) }, add = TRUE)
  failures <- character(0)
  for (reg in c("CTIS", "ISRCTN")) {
    url <- .safe_ctrdata_url(query, reg, days_back)
    tryCatch(ctrdata::ctrLoadQueryIntoDb(queryterm = url, con = con),
      error = function(e) { failures <<- c(failures, paste(reg, conditionMessage(e))) })
  }
  recs <- tryCatch(ctrdata::dbGetFieldsIntoDf(fields = c("_id", "ctrname"), con = con),
    error = function(e) { failures <<- c(failures, "Unable to read CTIS/ISRCTN import"); NULL })
  other <- .empty_signals()
  if (!is.null(recs) && nrow(recs)) {
    other <- data.frame(signal_type = "trial_registration",
      source = tolower(recs$ctrname %||% "registry"), ext_id = recs$`_id`,
      title = recs$`_id`, summary = "Registry lead; consult the linked record for dates and results.",
      org = "", signal_date = NA_character_, url = .trial_url(recs$`_id`, recs$ctrname),
      confidence = "authoritative", extra = "{}", score = 0, stringsAsFactors = FALSE)
  }
  if (length(failures)) other <- .fetch_issue(other, paste(failures, collapse = "; "))
  other
}

# Build a register-native query URL for ctrdata (it wants the portal URL).
.safe_ctrdata_url <- function(query, register, days_back) {
  enc <- utils::URLencode(query, reserved = TRUE)
  switch(register,
    CTGOV2 = sprintf("https://clinicaltrials.gov/search?term=%s", enc),
    CTIS   = sprintf("https://euclinicaltrials.eu/ctis-public/search?searchCriteria=%%7B%%22containAll%%22:%%22%s%%22%%7D", enc),
    ISRCTN = sprintf("https://www.isrctn.com/search?q=%s", enc),
    NULL)
}
.trial_url <- function(id, reg) {
  ifelse(grepl("^NCT", id), paste0("https://clinicaltrials.gov/study/", id),
  ifelse(grepl("ISRCTN", id), paste0("https://www.isrctn.com/", id),
         paste0("https://euclinicaltrials.eu/ctis-public/view/", id)))
}

# ============================================================
# TIER 2 — patents (free APIs, fiddly relevance)
# ============================================================

# --- PatentsView (USPTO) --------------------------------------
# Free API. Patent relevance to a disease is hard (mechanism language),
# so we search title+abstract for the terms and tag confidence 'reported'.
fetch_patentsview <- function(q, days_back, settings) {
  terms <- .signal_terms(q)
  if (length(terms) == 0) return(.empty_signals())
  key <- settings$signal_keys$patentsview %||% ""
  if (!nzchar(key)) stop("PatentsView requires a configured API key")
  from <- format(Sys.Date() - days_back, "%Y-%m-%d")
  # PatentsView query DSL (POST). _text_any over title+abstract.
  query <- list(`_and` = list(
    list(`_gte` = list(patent_date = from)),
    list(`_text_any` = list(patent_abstract = paste(terms, collapse = " ")))
  ))
  body <- list(q = query,
               f = list("patent_id","patent_title","patent_abstract",
                        "patent_date","assignees.assignee_organization"),
               o = list(size = 100))
  resp <- httr2::request("https://search.patentsview.org/api/v1/patent/") |>
    httr2::req_headers(`Content-Type` = "application/json", `X-Api-Key` = key) |>
    httr2::req_timeout(30) |>
    httr2::req_body_json(body) |>
    httr2::req_retry(max_tries = 2) |>
    httr2::req_perform()
  js <- httr2::resp_body_json(resp)
  pats <- js$patents %||% js$data$patents
  if (is.null(pats) || length(pats) == 0) return(.empty_signals())
  out <- do.call(rbind, lapply(pats, function(p) {
    org <- ""
    if (!is.null(p$assignees) && length(p$assignees))
      org <- p$assignees[[1]]$assignee_organization %||% ""
    data.frame(
      signal_type = "patent", source = "patentsview",
      ext_id = p$patent_id %||% "",
      title = p$patent_title %||% "",
      summary = substr(p$patent_abstract %||% "", 1, 1500),
      org = org, signal_date = p$patent_date %||% "",
      url = paste0("https://patents.google.com/patent/US", p$patent_id %||% ""),
      confidence = "reported", extra = "{}",
      score = 0, stringsAsFactors = FALSE
    )
  }))
  if (length(pats) >= 100L) out <- .fetch_issue(out, "PatentsView page limit reached; retrieval incomplete")
  out
}

# --- EPO OPS (European patents) -------------------------------
# Requires a free OPS key (OAuth). Stored encrypted per question.
# The protocol adapter is not yet implemented.
fetch_epo_ops <- function(q, days_back, settings) {
  stop("EPO OPS is not implemented; disable it until a verified adapter is installed")
}

# ============================================================
# TIER 3 — scrapers (framework + seeded targets; YOU add targets)
# ============================================================

# --- Generic scrape target runner -----------------------------
# Each target: list(name, url, type='rss'|'html', selector=NULL).
# RSS targets are parsed structurally; HTML targets use a CSS selector
# to extract headline links. Scrapers are inherently fragile — every
# result is confidence='unverified' (a lead, not a fact).
fetch_scrape_target <- function(target, q, days_back) {
  if (!is.list(target) || length(target$url) != 1L ||
      is.na(target$url) || !grepl("^https?://", target$url))
    stop("A scrape target requires an HTTP(S) URL")
  if (length(target$type) != 1L || !target$type %in% c("rss", "html"))
    stop("A scrape target type must be rss or html")
  target$name <- target$name %||% ""
  terms <- tolower(.signal_terms(q))
  matches_terms <- function(text) {
    t <- tolower(text)
    any(vapply(terms, function(term) grepl(term, t, fixed = TRUE), logical(1)))
  }
  out <- .empty_signals()
  if (identical(target$type, "rss")) {
    feed <- .safe(xml2::read_xml(target$url), paste("rss", target$name))
    if (inherits(feed, "data.frame")) return(.fetch_issue(out, attr(feed, "fetch_error") %||% "RSS fetch failed"))
    items <- xml2::xml_find_all(feed, "//item")
    for (it in items) {
      title <- xml2::xml_text(xml2::xml_find_first(it, "title"))
      link  <- xml2::xml_text(xml2::xml_find_first(it, "link"))
      desc  <- xml2::xml_text(xml2::xml_find_first(it, "description"))
      pub   <- xml2::xml_text(xml2::xml_find_first(it, "pubDate"))
      if (is.na(link) || !nzchar(link)) next
      link <- xml2::url_absolute(link, target$url)
      if (!grepl("^https?://", link)) next
      date <- .parse_rss_date(pub)
      if (!is.na(date) && as.Date(date) < Sys.Date() - days_back) next
      if (matches_terms(paste(title, desc))) {
        out <- rbind(out, data.frame(
          signal_type = "press_release",
          source = paste0("press:", .domain(target$url)),
          ext_id = link, title = title %||% "",
          summary = substr(desc %||% "", 1, 1500), org = target$name,
          signal_date = date, url = link,
          confidence = "unverified", extra = "{}",
          score = 0, stringsAsFactors = FALSE))
      }
    }
  } else if (identical(target$type, "html")) {
    page <- .safe(rvest::read_html(target$url), paste("html", target$name))
    if (inherits(page, "data.frame")) return(.fetch_issue(out, attr(page, "fetch_error") %||% "HTML fetch failed"))
    sel <- target$selector %||% "a"
    links <- rvest::html_elements(page, sel)
    for (lk in links) {
      title <- trimws(rvest::html_text(lk))
      href  <- rvest::html_attr(lk, "href")
      if (!is.na(title) && nzchar(title) && matches_terms(title) && !is.na(href) && nzchar(href)) {
        href <- xml2::url_absolute(href, target$url)
        if (!grepl("^https?://", href)) next
        out <- rbind(out, data.frame(
          signal_type = "press_release",
          source = paste0("press:", .domain(target$url)),
          ext_id = href %||% title, title = title,
          summary = "", org = target$name,
          signal_date = NA_character_, url = href %||% target$url,
          confidence = "unverified", extra = "{}",
          score = 0, stringsAsFactors = FALSE))
      }
    }
  }
  out
}

.domain <- function(url) {
  m <- regmatches(url, regexpr("https?://([^/]+)", url))
  gsub("https?://", "", m)
}
.parse_rss_date <- function(x) {
  if (is.null(x) || length(x) == 0 || is.na(x) || !nzchar(x)) return(NA_character_)
  d <- suppressWarnings(as.Date(x, format = "%a, %d %b %Y"))
  if (is.na(d)) d <- .parse_date(x)
  as.character(d)
}

# ============================================================
# Curated RSS/Atom feeds (journals, institutions, aggregators,
# trade press, news). Reads config/feeds.yaml. Each feed declares
# its own signal_type + confidence, so a journal online-first feed
# is NOT lumped with a scraped press release. Items are matched
# against the question's concept terms.
# ============================================================
fetch_curated_feeds <- function(q, sigcfg, days_back, feeds_path = "config/feeds.yaml") {
  if (!file.exists(feeds_path)) stop("Curated feeds configuration does not exist")
  reg <- yaml::read_yaml(feeds_path)$feeds
  if (is.null(reg) || length(reg) == 0) stop("Curated feeds configuration is empty")

  # Which feed categories has this question enabled? Default: all of them.
  enabled_cats <- sigcfg$feed_categories
  cat_on <- function(ct) {
    if (is.null(enabled_cats) || length(enabled_cats) == 0) return(TRUE)
    isTRUE(enabled_cats[[ct]])
  }

  terms <- tolower(.signal_terms(q))
  matches <- function(text) {
    t <- tolower(text)
    any(vapply(terms, function(tm) grepl(tm, t, fixed = TRUE), logical(1)))
  }
  cutoff <- Sys.Date() - days_back

  out <- .empty_signals(); failures <- character(0)
  for (feed in reg) {
    if (!cat_on(feed$signal_type)) next
    parsed <- .safe(xml2::read_xml(feed$url), paste("feed", feed$name))
    if (inherits(parsed, "data.frame")) {
      failures <- c(failures, paste(feed$name, attr(parsed, "fetch_error")))
      next
    }

    # Support both RSS (<item>) and Atom (<entry>).
    items <- xml2::xml_find_all(parsed, "//item")
    is_atom <- FALSE
    if (length(items) == 0) {
      items <- xml2::xml_find_all(parsed, "//*[local-name()='entry']")
      is_atom <- TRUE
    }
    for (it in items) {
      if (is_atom) {
        title <- xml2::xml_text(xml2::xml_find_first(it, ".//*[local-name()='title']"))
        link  <- xml2::xml_attr(xml2::xml_find_first(it, ".//*[local-name()='link']"), "href")
        desc  <- xml2::xml_text(xml2::xml_find_first(it, ".//*[local-name()='summary']"))
        pub   <- xml2::xml_text(xml2::xml_find_first(it, ".//*[local-name()='updated']"))
      } else {
        title <- xml2::xml_text(xml2::xml_find_first(it, "title"))
        link  <- xml2::xml_text(xml2::xml_find_first(it, "link"))
        desc  <- xml2::xml_text(xml2::xml_find_first(it, "description"))
        pub   <- xml2::xml_text(xml2::xml_find_first(it, "pubDate"))
      }
      if (is.na(title) || !nzchar(title %||% "")) next
      if (!matches(paste(title, desc))) next
      sdate <- .parse_feed_date(pub)
      # date filter (keep if undated or within window)
      if (!is.na(suppressWarnings(as.Date(sdate))) &&
          as.Date(sdate) < cutoff) next
      out <- rbind(out, data.frame(
        signal_type = feed$signal_type %||% "news",
        source = paste0("feed:", .domain(feed$url)),
        ext_id = link %||% title,
        title = title,
        summary = substr(desc %||% "", 1, 1500),
        org = feed$name %||% .domain(feed$url),
        signal_date = sdate, url = link %||% feed$url,
        confidence = feed$confidence %||% "unverified",
        extra = "{}", score = 0, stringsAsFactors = FALSE
      ))
    }
  }
  if (length(failures)) out <- .fetch_issue(out, paste(failures, collapse = "; "))
  out
}

.parse_feed_date <- function(x) {
  # Do not manufacture a fresh event date for unchanged undated feed items.
  .parse_rss_date(x)
}

# ============================================================
# Orchestrate all signal sources for one question
# ============================================================
fetch_signals <- function(q, sigcfg, settings) {
  days <- q$from_days_back %||% 7
  safe_signal <- function(expr, label) .safe(expr, label, empty = .empty_signals,
                                            secrets = settings$signal_keys)
  on <- function(name) {
    v <- sigcfg$sources[[name]]
    isTRUE(v)
  }
  out <- list()
  if (on("sec_edgar"))     out$sec  <- safe_signal(fetch_sec_edgar(q, days, settings), "sec_edgar")
  if (on("nih_reporter"))  out$nih  <- safe_signal(fetch_nih_reporter(q, days, settings), "nih_reporter")
  if (on("openfda"))       out$fda  <- safe_signal(fetch_openfda(q, days, settings), "openfda")
  if (on("trials"))        out$tr   <- safe_signal(fetch_trials_ctrdata(q, days, settings), "trials")
  if (on("patentsview"))   out$pv   <- safe_signal(fetch_patentsview(q, days, settings), "patentsview")
  if (on("epo_ops"))       out$epo  <- safe_signal(fetch_epo_ops(q, days, settings), "epo_ops")

  # curated RSS/Atom feeds (journals, institutions, aggregators, trade, news)
  if (on("feeds") || length(sigcfg$feed_categories)) {
    out$feeds <- safe_signal(fetch_curated_feeds(q, sigcfg, days), "curated_feeds")
  }

  # Commercial adapters remain unavailable until their contracted endpoints
  # and response formats are verified. Enabled stubs report source failure.
  if (on("cortellis"))
    out$cortellis <- safe_signal(fetch_pharma_intel("cortellis", q, days, settings), "cortellis")
  if (on("citeline"))
    out$citeline  <- safe_signal(fetch_pharma_intel("citeline", q, days, settings), "citeline")

  # scrape targets (each its own .safe)
  if (length(sigcfg$scrape_targets)) {
    for (i in seq_along(sigcfg$scrape_targets)) {
      tgt <- sigcfg$scrape_targets[[i]]
      out[[paste0("scrape_", i)]] <- safe_signal(
        fetch_scrape_target(tgt, q, days), paste0("scrape:", tgt$name %||% i))
    }
  }
  res <- .fetch_bind(out, empty = .empty_signals)
  attr(res, "source_counts") <- lapply(out, .fetch_health)
  res
}
