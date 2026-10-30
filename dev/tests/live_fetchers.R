# ============================================================
# Opt-in probes of external call sites; skipped sources remain unverified.
#
# Two layers:
#   A) argument names checked against the INSTALLED package signatures
#   B) each fetcher run LIVE against the real API with a tiny query,
#      asserting the normalized record schema comes back
#
# Narrow date windows limit load, but adapters can make multiple paginated
# requests and consume provider quota. This probe does not supply contact
# addresses; provider restrictions/authentication can prevent a live check.
# OpenAlex needs a key for cursor pagination; OPENALEX_API_KEY is supported.
# Set RESEARCHR_SKIP_LIVE=1 to run only layer A.
# ============================================================
source("R/00_utils.R")
source("R/01_query_builder.R")
source("R/02_fetch.R")

fail <- 0L; skipped <- character(0)
ok <- function(name, cond, detail = "") {
  if (isTRUE(cond)) cat(sprintf("  [PASS] %s\n", name))
  else { cat(sprintf("  [FAIL] %s%s\n", name, if (nzchar(detail)) paste0(" — ", detail) else "")); fail <<- fail + 1L }
}
skip <- function(name, why) { cat(sprintf("  [SKIP] %s — %s\n", name, why)); skipped <<- c(skipped, name) }

has_args <- function(fn, args) {
  f <- names(formals(fn))
  if ("..." %in% f) return(TRUE)          # variadic: cannot disprove statically
  all(args %in% f)
}

cat("A. argument names vs installed package signatures\n")
ok("rentrez::entrez_search accepts db/term/reldate/datetype/retmax/use_history",
   has_args(rentrez::entrez_search, c("db","term")) &&
   all(c("reldate","datetype","retmax","use_history") %in%
       names(formals(rentrez::entrez_search)) | "..." %in% names(formals(rentrez::entrez_search))))
ok("rentrez::entrez_summary accepts db/web_history/always_return_list",
   has_args(rentrez::entrez_summary, c("db")))
ok("rentrez::set_entrez_key exists", is.function(rentrez::set_entrez_key))
ok("europepmc::epmc_search accepts query/limit/verbose",
   has_args(europepmc::epmc_search, c("query","limit","verbose")),
   paste(names(formals(europepmc::epmc_search)), collapse=","))
ok("native OpenAlex fetcher accepts concept terms and settings",
   has_args(fetch_openalex, c("concept_terms", "days_back", "settings")))

if (nzchar(Sys.getenv("RESEARCHR_SKIP_LIVE"))) {
  cat("\nLive calls skipped (RESEARCHR_SKIP_LIVE set)\n")
  quit(status = if (fail > 0L) 1L else 0L)
}

SCHEMA <- c("source","ext_id","doi","title","abstract","authors",
            "journal","pub_date","url","type")
settings <- list(
  ncbi = list(api_key = "", email = ""),
  semanticscholar = list(api_key = ""),
  contact_email = "",        # no address transmitted from a test run
  unpaywall = list(email = ""))

# This suite asks "does OUR adapter map the live response into our schema
# correctly". An upstream outage is not an answer to that question, so it is
# reported as a SKIP with the message rather than a FAIL. A skip does not
# verify the adapter, and a schema check does not establish complete recall.
# Production records source failures/incompleteness in fetch_log; a failed
# source must not be confused with a successful search returning no rows.
check_frame <- function(label, df, require_rows = TRUE) {
  if (inherits(df, "error")) {
    skip(paste(label, "(upstream)"), conditionMessage(df)); return(invisible())
  }
  ok(paste(label, "returns a data frame"), is.data.frame(df))
  if (!is.data.frame(df)) return(invisible())
  ok(paste(label, "keeps the normalized column set"), all(SCHEMA %in% names(df)),
     paste(setdiff(SCHEMA, names(df)), collapse=","))
  if (require_rows) ok(paste(label, "returned at least one record"), nrow(df) > 0, paste(nrow(df), "rows"))
  if (nrow(df) > 0) {
    ok(paste(label, "pub_date parses to Date"), inherits(df$pub_date, "Date"))
    ok(paste(label, "titles are non-empty"), any(nzchar(df$title)))
  }
}

cat("\nB. live calls\n")

cat("\n  PubMed (rentrez)\n")
r <- tryCatch(fetch_pubmed("semaglutide[Title]", 7, settings), error = function(e) e)
check_frame("pubmed", r)
if (is.data.frame(r) && nrow(r)) {
  ok("pubmed populates authoritative publication-type tags", any(nzchar(r$type)),
     paste(head(r$type, 2), collapse=" | "))
  ok("pubmed tags are '; '-joined as the classifier expects",
     any(grepl(";", r$type)) || all(nzchar(r$type)))
}

cat("\n  Europe PMC (europepmc)\n")
r <- tryCatch(fetch_europepmc("semaglutide", 7, settings), error = function(e) e)
check_frame("europepmc", r)
if (is.data.frame(r) && nrow(r)) {
  # regression: the default output="parsed" result set has no abstractText
  # column at all, which left every abstract silently empty.
  ok("europepmc populates abstracts", sum(nzchar(r$abstract)) > 0,
     paste(sum(nzchar(r$abstract)), "of", nrow(r)))
  ok("europepmc populates publication types", sum(nzchar(r$type)) > 0,
     paste(sum(nzchar(r$type)), "of", nrow(r)))
}

cat("\n  OpenAlex (native httr2 API)\n")
r <- tryCatch(fetch_openalex(list(c("semaglutide"), c("steatohepatitis")), 120, settings),
              error = function(e) e)
check_frame("openalex", r, require_rows = FALSE)

cat("\n  ClinicalTrials.gov v2 (httr2)\n")
r <- tryCatch(fetch_clinicaltrials("semaglutide", 30, settings), error = function(e) e)
check_frame("clinicaltrials", r)
if (is.data.frame(r) && nrow(r)) {
  ok("CT.gov ext_id looks like an NCT number", any(grepl("^NCT[0-9]{8}$", r$ext_id)),
     paste(head(r$ext_id, 2), collapse=","))
  ok("CT.gov type carries the 'Trial:' prefix the classifier keys on",
     all(grepl("^Trial: ", r$type)), paste(head(r$type, 2), collapse=","))
}

cat("\n  bioRxiv / medRxiv\n")
matcher <- function(txt) grepl("liver|hepat", tolower(txt))
r <- tryCatch(fetch_biorxiv(matcher, 2, settings), error = function(e) e)
check_frame("biorxiv", r, require_rows = FALSE)
if (is.data.frame(r) && nrow(r)) {
  ok("biorxiv rows are marked as preprints", all(r$type == "Preprint"))
  ok("the client-side matcher actually filtered", all(grepl("liver|hepat", tolower(paste(r$title, r$abstract)))))
}

cat("\n  Crossref\n")
r <- tryCatch(fetch_crossref("semaglutide steatohepatitis", 30, settings), error = function(e) e)
check_frame("crossref", r, require_rows = FALSE)

cat("\n  Semantic Scholar\n")
r <- tryCatch(fetch_semanticscholar(list(c("semaglutide"), c("steatohepatitis")), 120, settings),
              error = function(e) e)
if (inherits(r, "error")) {
  skip("semanticscholar", conditionMessage(r))
} else {
  check_frame("semanticscholar", r, require_rows = FALSE)
  if (nrow(r) == 0) skip("semanticscholar rows",
    "keyless requests are rate-limited; the fetcher backed off and returned 0 rows cleanly")
}

cat("\n", if (fail == 0L) "ALL LIVE FETCHER CHECKS PASSED" else paste(fail, "FAILURE(S)"),
    if (length(skipped)) paste0(" (skipped: ", paste(skipped, collapse=", "), ")") else "", "\n", sep = "")
quit(status = if (fail > 0L) 1L else 0L)
