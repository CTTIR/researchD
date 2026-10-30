# Regression checks for scientific classification, retrieval and persistence.
# All upstream responses are synthetic; this file never sends network requests.
source("R/00_utils.R")
source("R/01_query_builder.R")
source("R/02_fetch.R")
source("R/03_dedup.R")
source("R/04_rank.R")
source("R/06_classify.R")
source("R/07_db_questions.R")
source("R/08_signals.R")
source("R/09_signals_persist.R")
source("R/10_pharma_intel.R")

check <- function(name, ok) {
  if (!isTRUE(ok)) stop(name, call. = FALSE)
  cat("PASS: ", name, "\n", sep = "")
}
mock_http <- function(expr, response, status = 200L) {
  testthat::with_mocked_bindings(expr,
    req_perform = function(req, ...) req,
    resp_body_json = response,
    resp_status = function(...) status,
    .package = "httr2")
}
settings <- list(fetch = list(max_records = 5), sources = list(),
  ncbi = list(api_key = ""), semanticscholar = list(api_key = ""),
  ranking = list(weight_recency = 1, weight_source = 0.5, weight_relevance = 2,
    source_priority = list(pubmed = 2, europepmc = 2)))
q <- list(id = "q", concepts = list(list(name = "drug", terms = c("alpha", "beta"))),
  sources = list(semanticscholar = TRUE), api_keys = list(), from_days_back = 7)
disabled <- q; disabled$sources <- list()
empty_lit <- fetch_question(disabled, build_all_queries(disabled), settings)
empty_sig <- fetch_signals(disabled, list(sources = list()), settings)
check("disabled literature sources produce a typed empty batch", is.data.frame(empty_lit) &&
  nrow(empty_lit) == 0L && identical(names(empty_lit), names(.empty_records())) &&
  length(attr(empty_lit, "source_counts")) == 0L)
check("disabled signal sources produce a typed empty batch", is.data.frame(empty_sig) &&
  nrow(empty_sig) == 0L && identical(names(empty_sig), names(.empty_signals())) &&
  length(attr(empty_sig, "source_counts")) == 0L)
missing_core <- testthat::with_mocked_bindings(
  tryCatch(ensure_packages("researchdMissingDependencyTest"), error = identity),
  install.packages = function(...) stop("unexpected runtime installation"), .package = "utils")
check("missing core dependencies fail with preparation instructions and no runtime install",
  inherits(missing_core, "error") && grepl("researchdMissingDependencyTest", conditionMessage(missing_core)) &&
  grepl("Install them before starting", conditionMessage(missing_core), fixed = TRUE))

testthat::with_mocked_bindings({
  out <- fetch_question(q, build_all_queries(q), settings)
  check("nested Semantic Scholar failures reach source health", !attr(out, "source_counts")$s2$ok)
}, req_perform = function(...) stop("synthetic outage"), .package = "httr2")
original_sec <- fetch_sec_edgar
fetch_sec_edgar <- function(...) stop("synthetic signal outage")
out <- fetch_signals(q, list(sources = list(sec_edgar = TRUE)), settings)
fetch_sec_edgar <- original_sec
check("signal failures retain zero-row signal schema and error", identical(names(out), names(.empty_signals())) &&
  !attr(out, "source_counts")$sec$ok)
check("unimplemented adapters fail visibly", inherits(tryCatch(fetch_epo_ops(q, 7, settings), error = identity), "error") &&
  inherits(tryCatch(fetch_pharma_intel("citeline", q, 7, settings), error = identity), "error"))

check("MeSH-only PubMed block has no empty tiab term",
  identical(build_pubmed(list(concepts = list(list(mesh = "Glioma")))), "(Glioma[MeSH Terms])"))
records <- data.frame(source = "pubmed", journal = "J",
  title = c("A controlled trial", "An animal experiment", "A multicenter analysis", "A nonrandomized trial"),
  type = c("Controlled Clinical Trial", "Journal Article", "Multicenter Study", ""))
classified <- classify_study_type(records)
check("study labels require evidence of design", identical(classified$study_type,
  c("clinical_trial", "unclassified", "unclassified", "clinical_trial")))

mk_study <- function(id) list(protocolSection = list(
  identificationModule = list(nctId = id, briefTitle = id),
  statusModule = list(lastUpdatePostDateStruct = list(date = as.character(Sys.Date())))))
requests <- character(0)
ct <- mock_http(fetch_clinicaltrials("alpha", 7, settings), function(req, ...) {
  requests <<- c(requests, req$url)
  if (length(requests) == 1) list(studies = list(mk_study("NCT1")), nextPageToken = "two")
  else list(studies = list(mk_study("NCT2")))
})
check("CTGOV follows continuation tokens", nrow(ct) == 2L && grepl("pageToken=two", requests[2]))
trial <- mk_study("NCT1")
trial$protocolSection$statusModule$studyFirstPostDateStruct <- list(date = "2020-03-04")
trial$protocolSection$statusModule$resultsFirstPostDateStruct <- list(date = "2026-10-29")
trial$hasResults <- TRUE
events <- .ctgov_signals(list(trial))
check("trial registration and posted results are distinct dated events",
  identical(events$signal_type, c("trial_registration", "trial_result")) &&
  identical(events$signal_date, c("2020-03-04", "2026-10-29")) &&
  !anyDuplicated(events$ext_id))
limited <- settings; limited$fetch$max_records <- 1L
ct <- mock_http(fetch_clinicaltrials("alpha", 7, limited), function(...) {
  list(studies = list(mk_study("NCT1")), nextPageToken = "more")
})
check("a retrieval cap is visible rather than a successful complete fetch", !is.null(attr(ct, "fetch_error")))
requests <- character(0)
ct <- mock_http(fetch_clinicaltrials("alpha", 7, settings), function(req, ...) {
  requests <<- c(requests, req$url)
  if (length(requests) == 1) list(studies = list(mk_study("NCT1")), nextPageToken = "two")
  else stop("synthetic second-page outage")
})
check("an unavailable later page preserves earlier records and reports failure",
  nrow(ct) == 1L && !is.null(attr(ct, "fetch_error")))

paper <- function(id) list(paperId = id, title = id, publicationDate = as.character(Sys.Date()))
s2 <- mock_http(fetch_semanticscholar(list(c("alpha", "beta"), "disease"), 7, settings), function(req, ...) {
  query <- httr2::url_parse(req$url)$query$query
  hits <- switch(query, alpha = list(paper("A")), beta = list(paper("B")),
    disease = list(paper("A"), paper("B")))
  list(total = length(hits), data = hits)
})
check("synonyms union before concept intersection", setequal(s2$ext_id, c("A", "B")))
keyed_q <- q; keyed_q$api_keys$semanticscholar <- "synthetic-question-key"
invisible(mock_http(fetch_question(keyed_q, build_all_queries(keyed_q), settings), function(req, ...) {
  check("per-question Semantic Scholar key reaches the request",
    identical(req$headers[["x-api-key"]], "synthetic-question-key"))
  list(total = 0L, data = list())
}))

oa_work <- function(id) list(id = paste0("https://openalex.org/", id),
  display_name = id, publication_date = as.character(Sys.Date()),
  abstract_inverted_index = list(Useful = list(0L), abstract = list(1L)),
  authorships = list(list(author = list(display_name = "A Author"))),
  primary_location = list(source = list(display_name = "Journal")))
requests <- character(0)
oa_settings <- settings; oa_settings$openalex$api_key <- "synthetic-openalex-key"
oa <- mock_http(fetch_openalex(list(c("alpha", "beta"), "disease"), 7, oa_settings), function(req, ...) {
  requests <<- c(requests, req$url)
  params <- httr2::url_parse(req$url)$query
  check("OpenAlex sends supported page size, Boolean blocks and key",
    as.integer(params$per_page) <= 100L &&
    identical(params$search.title_and_abstract, "(alpha OR beta) AND (disease)") &&
    !is.null(req$headers[["Authorization"]]) && !grepl("synthetic-openalex-key", req$url))
  if (length(requests) == 1L) list(meta = list(count = 2L, next_cursor = "two"), results = list(oa_work("W1")))
  else list(meta = list(count = 2L), results = list(oa_work("W2")))
})
check("OpenAlex paginates and restores abstract metadata",
  nrow(oa) == 2L && identical(oa$abstract, rep("Useful abstract", 2L)) &&
    grepl("cursor=two", requests[2]))
oa <- mock_http(fetch_openalex(list("alpha"), 7, settings), function(req, ...) {
  list(meta = list(count = 20L, next_cursor = "two"), results = list(oa_work("W1")))
})
check("keyless OpenAlex never claims completeness beyond its available page",
  nrow(oa) == 1L && !is.null(attr(oa, "fetch_error")))

requests <- character(0)
crossref <- mock_http(fetch_crossref("alpha", 7, settings), function(req, ...) {
  requests <<- c(requests, req$url)
  if (length(requests) == 1L) list(message = list(`total-results` = 2L, `next-cursor` = "two",
    items = list(list(DOI = "10.1/a", title = list("A"), published = list(`date-parts` = list(list(2026L, 10L, 1L)))))))
  else list(message = list(`total-results` = 2L,
    items = list(list(DOI = "10.1/b", title = list("B")))))
})
check("Crossref cursor pages retain separate works", nrow(crossref) == 2L && grepl("cursor=two", requests[2]))

bio <- testthat::with_mocked_bindings(fetch_biorxiv(function(...) TRUE, 7, settings),
  fromJSON = function(url, ...) {
    if (grepl("/biorxiv/", url)) stop("synthetic bioRxiv outage")
    list(messages = list(total = 1L), collection = data.frame(doi = "10.1/p", title = "A",
      abstract = "B", authors = "C", date = as.character(Sys.Date())))
  }, .package = "jsonlite")
check("one preprint server cannot erase another server failure", nrow(bio) == 1L &&
  bio$source == "medrxiv" && !is.null(attr(bio, "fetch_error")))

nih <- mock_http(fetch_nih_reporter(q, 7, settings), function(req, ...) {
  list(meta = list(total = 20L), results = list(list(appl_id = 123L,
    project_num = "5R01EXAMPLE-01", project_title = "An award")))
})
check("NIH links use application IDs and disclose first-page truncation",
  identical(nih$url, "https://reporter.nih.gov/project-details/123") &&
  !is.null(attr(nih, "fetch_error")))

fda <- mock_http(fetch_openfda(q, 7, settings), function(req, ...) {
  check("FDA request constrains effective date", grepl("effective_time", req$url))
  list(meta = list(results = list(total = 1)), results = list(list(
    set_id = "stable-label", effective_time = format(Sys.Date(), "%Y%m%d"),
    openfda = list(brand_name = list("Example"), application_number = list("NDA1")))))
})
fda <- rank_signals(fda, q, settings)
check("compact FDA dates rank as ISO dates", identical(fda$signal_date, as.character(Sys.Date())) && is.finite(fda$score))
undated <- fda; undated$signal_date <- "not-a-date"
check("invalid and absent dates cannot abort a signals batch", is.finite(rank_signals(undated, q, settings)$score))
check("undated signals never receive an invented current date",
  is.na(.parse_feed_date("")) && is.na(.parse_rss_date("not-a-date")))
check("PatentSearch missing credentials is explicit", inherits(tryCatch(fetch_patentsview(q,7,settings),error=identity),"error"))
patent_settings <- settings; patent_settings$signal_keys$patentsview <- "synthetic-patent-key"
invisible(mock_http(fetch_patentsview(q, 7, patent_settings), function(req, ...) {
  check("PatentSearch sends the configured header key",
    identical(req$headers[["X-Api-Key"]], "synthetic-patent-key"))
  list(patents = list())
}))
unknown <- undated; unknown$signal_type <- "custom_lead"
check("an unknown signal category has a finite fallback score", is.finite(rank_signals(unknown, q, settings)$score))

con <- DBI::dbConnect(RSQLite::SQLite(), ":memory:")
db_init_schema(con, "db/schema.sql")
DBI::dbExecute(con, "INSERT INTO questions(id,label,delivery) VALUES('q','Q','[\"zotero\",\"file\"]')")
first <- persist_signals(con, fda, "q")
check("first signal is stored", nrow(first) == 1L)
fda$score <- fda$score + 1
check("score drift does not manufacture a signal event", nrow(persist_signals(con,fda,"q")) == 0L)
fda$title <- "Changed label"
second <- persist_signals(con,fda,"q")
check("changed signal is a new event preserving the original", nrow(second) == 1L &&
  DBI::dbGetQuery(con,"SELECT COUNT(*) AS n FROM signals")$n == 2L)
fda$title <- "FDA label: Example"
check("reappearance of an older revision is idempotent", nrow(persist_signals(con,fda,"q")) == 0L)
fda$extra <- '{"phase":2,"details":{"a":1,"b":2}}'
persist_signals(con, fda, "q")
fda$extra <- '{"details":{"b":2,"a":1},"phase":2}'
check("JSON object key order does not manufacture revisions", nrow(persist_signals(con,fda,"q")) == 0L)
fda$extra <- '{"different":2,"details":{"a":1,"b":2}}'
check("JSON key-name changes are material signal updates", nrow(persist_signals(con,fda,"q")) == 1L)
fda$extra <- '{"value":1}'
invisible(persist_signals(con, fda, "q"))
fda$extra <- '{"value":1.000000001}'
check("small numerical metadata changes remain visible as revisions", nrow(persist_signals(con,fda,"q")) == 1L)
log_fetch(con,"q","signals",attr(out,"source_counts"))
check("failed signals source is persisted as failed", DBI::dbGetQuery(con,"SELECT ok FROM fetch_log")$ok == 0L)

raw <- data.frame(source = c("pubmed", "europepmc"), ext_id = c("1", "1"),
  doi = "10.1/example", title = "Example", abstract = c("", "Useful abstract"),
  authors = "A", journal = "J", pub_date = Sys.Date(), url = "https://example.org",
  type = "Journal Article", study_type = "unclassified", is_preprint = 0L, score = 1)
merged <- persist_records(con, raw, "q", settings$ranking$source_priority)
check("duplicate enrichment retains primary source and abstract", nrow(merged) == 1L &&
  merged$source == "pubmed" && merged$abstract == "Useful abstract")
prov <- jsonlite::fromJSON(merged$provenance, simplifyVector = FALSE)
check("enriched field has source provenance", length(prov) == 2L && identical(prov[[2]]$fields[[1]], "abstract"))
check("selected delivery channels and record are atomically queued",
  setequal(DBI::dbGetQuery(con,"SELECT channel FROM delivery_outbox")$channel, c("zotero", "file")))
check("repeat fetch neither redelivers nor duplicates queue", nrow(persist_records(con,raw,"q",settings$ranking$source_priority)) == 0L &&
  DBI::dbGetQuery(con,"SELECT COUNT(*) AS n FROM delivery_outbox")$n == 2L)
DBI::dbExecute(con, "UPDATE records SET abstract='', provenance='[]'")
persist_records(con, raw[2L, , drop = FALSE], "q", settings$ranking$source_priority)
backfilled <- DBI::dbGetQuery(con, "SELECT abstract, provenance FROM records")
prov <- jsonlite::fromJSON(backfilled$provenance, simplifyVector = FALSE)
check("later metadata enrichment retains field attribution without redelivery",
  backfilled$abstract == "Useful abstract" && prov[[1L]]$source == "europepmc" &&
  "abstract" %in% unlist(prov[[1L]]$fields) &&
  DBI::dbGetQuery(con,"SELECT COUNT(*) AS n FROM delivery_outbox")$n == 2L)
DBI::dbExecute(con, "DROP TABLE delivery_outbox")
changed <- raw[1L, , drop = FALSE]; changed$doi <- "10.1/rollback"
check("record insertion rolls back if durable delivery enqueue fails",
  inherits(tryCatch(persist_records(con, changed, "q", settings$ranking$source_priority), error=identity), "error") &&
  DBI::dbGetQuery(con, "SELECT COUNT(*) AS n FROM records WHERE doi='10.1/rollback'")$n == 0L)
raw$doi <- NA_character_; raw$title <- c("One", "Two")
check("missing DOIs use individual titles rather than one NA key", nrow(.collapse_batch(raw,settings$ranking$source_priority)) == 2L)
raw$title <- c("α receptor", "β receptor")
unicode <- .collapse_batch(raw, settings$ranking$source_priority)
check("Unicode scientific terms cannot collapse after ASCII stripping",
  nrow(unicode) == 2L && all(startsWith(unicode$dedup_key, "tiu:")))
raw$title <- c("研究 \tTitle", "研究 Title")
check("identical Unicode titles normalize ASCII whitespace consistently",
  nrow(.collapse_batch(raw, settings$ranking$source_priority)) == 1L)
raw$title <- "Undated example"; raw$pub_date <- as.Date(NA)
check("missing-year title keys match external ingestion",
  identical(.collapse_batch(raw, settings$ranking$source_priority)$dedup_key, "ti:undated example|"))
DBI::dbDisconnect(con)
cat("All engine hardening regressions passed.\n")
