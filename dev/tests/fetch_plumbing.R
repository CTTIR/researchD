# ============================================================
# Phase 3 — fetch_question() source/key plumbing, .safe robustness,
# and the R -> web/decrypt_key.py seam.
# All source fetchers are replaced with mocks: no network is touched.
# ============================================================
source("R/00_utils.R")
source("R/01_query_builder.R")
source("R/02_fetch.R")
source("R/07_db_questions.R")

fail <- 0L
ok <- function(name, cond, detail = "") {
  if (isTRUE(cond)) cat(sprintf("  [PASS] %s\n", name))
  else { cat(sprintf("  [FAIL] %s%s\n", name, if (nzchar(detail)) paste0(" — ", detail) else "")); fail <<- fail + 1L }
}

called <- new.env(parent = emptyenv())
seen_key <- new.env(parent = emptyenv())

mock_row <- function(src) data.frame(
  source = src, ext_id = "1", doi = paste0("10.1/", src), title = paste("T", src),
  abstract = "", authors = "A", journal = "J", pub_date = Sys.Date(),
  url = "u", type = "", stringsAsFactors = FALSE)

install_mocks <- function(failing = character(0)) {
  rm(list = ls(called), envir = called); rm(list = ls(seen_key), envir = seen_key)
  for (nm in c("pubmed","europepmc","openalex","semanticscholar",
               "crossref","clinicaltrials","biorxiv")) {
    local({
      src <- nm
      fn <- function(query, days_back, settings) {
        assign(src, TRUE, envir = called)
        assign(src, settings$ncbi$api_key %||% "", envir = seen_key)
        if (src %in% failing) stop("simulated ", src, " outage")
        mock_row(src)
      }
      assign(paste0("fetch_", switch(src, europepmc = "europepmc", src)), fn,
             envir = globalenv())
    })
  }
}

settings <- list(
  sources = list(pubmed = TRUE, europepmc = TRUE, openalex = TRUE,
                 semanticscholar = TRUE, crossref = TRUE,
                 clinicaltrials = TRUE, biorxiv = TRUE),
  ncbi = list(api_key = "GLOBAL_NCBI_KEY", email = "x@y.z"),
  contact_email = "x@y.z")
queries <- list(pubmed = "q", europepmc = "q", openalex = list(), semanticscholar = "q",
                crossref = "q", clinicaltrials = "q", biorxiv_match = "q")

cat("1. per-question source toggles gate which fetchers run\n")
install_mocks()
q <- list(id = "q1", from_days_back = 14,
          sources = list(pubmed = TRUE, europepmc = FALSE, openalex = TRUE,
                         semanticscholar = FALSE, crossref = FALSE,
                         clinicaltrials = FALSE, biorxiv = FALSE),
          api_keys = list())
out <- fetch_question(q, queries, settings)
ok("only the enabled sources were called",
   setequal(ls(called), c("pubmed", "openalex")), paste(sort(ls(called)), collapse = ","))
ok("disabled sources contribute no rows", setequal(out$source, c("pubmed", "openalex")),
   paste(out$source, collapse = ","))
ok("source_counts is reported for the enabled sources only",
   setequal(names(attr(out, "source_counts")), c("pubmed", "oa")),
   paste(names(attr(out, "source_counts")), collapse = ","))
ok("a healthy source is marked ok",
   isTRUE(attr(out, "source_counts")$pubmed$ok))

cat("\n2. a question with no explicit toggle falls back to global settings\n")
install_mocks()
q2 <- list(id = "q2", sources = list(), api_keys = list())
out2 <- fetch_question(q2, queries, settings)
ok("all globally-enabled sources ran", length(ls(called)) == 7, paste(length(ls(called))))
settings_off <- settings; settings_off$sources$biorxiv <- FALSE
install_mocks()
fetch_question(q2, queries, settings_off)
ok("a globally-disabled source is skipped", !("biorxiv" %in% ls(called)))

cat("\n3. per-question API key overrides the global one\n")
install_mocks()
q3 <- list(id = "q3", sources = list(pubmed = TRUE), api_keys = list(pubmed = "PER_QUESTION_KEY"))
fetch_question(q3, queries, settings)
ok("the fetcher received the question's key, not the global one",
   identical(get("pubmed", envir = seen_key), "PER_QUESTION_KEY"),
   get("pubmed", envir = seen_key))
install_mocks()
q4 <- list(id = "q4", sources = list(pubmed = TRUE), api_keys = list(pubmed = ""))
fetch_question(q4, queries, settings)
ok("an empty per-question key falls back to the global key",
   identical(get("pubmed", envir = seen_key), "GLOBAL_NCBI_KEY"),
   get("pubmed", envir = seen_key))

cat("\n4. one source failing does not sink the run\n")
install_mocks(failing = c("openalex", "crossref"))
q5 <- list(id = "q5", sources = list(), api_keys = list())
out5 <- withCallingHandlers(fetch_question(q5, queries, settings),
                            message = function(m) invokeRestart("muffleMessage"))
ok("the run completed despite two failing sources", is.data.frame(out5))
ok("the five healthy sources still returned rows", nrow(out5) == 5, paste(nrow(out5)))
ok("the failing sources contributed nothing",
   !any(c("openalex", "crossref") %in% out5$source))
sc <- attr(out5, "source_counts")
ok("source_counts records 0 rows for a failed source",
   identical(sc$oa$n, 0L) && identical(sc$cr$n, 0L), paste(sc$oa$n, sc$cr$n))
ok("a FAILED fetch is distinguishable from an EMPTY one",
   isFALSE(sc$oa$ok) && isFALSE(sc$cr$ok) && isTRUE(sc$pubmed$ok),
   paste("oa.ok=", sc$oa$ok, " pubmed.ok=", sc$pubmed$ok))
ok("the failure reason is carried through for the log",
   grepl("simulated openalex outage", sc$oa$error), sc$oa$error)

cat("\n5. every source failing still yields a well-formed empty frame\n")
install_mocks(failing = c("pubmed","europepmc","openalex","semanticscholar",
                          "crossref","clinicaltrials","biorxiv"))
out6 <- withCallingHandlers(fetch_question(q5, queries, settings),
                            message = function(m) invokeRestart("muffleMessage"))
ok("total outage returns 0 rows, not an error", is.data.frame(out6) && nrow(out6) == 0)
ok("the empty frame keeps the normalized column set",
   all(c("source","ext_id","doi","title","abstract","authors","journal",
         "pub_date","url","type") %in% names(out6)), paste(names(out6), collapse = ","))

cat("\n6. the R -> decrypt_key.py seam\n")
key <- system2("python3", c("-c",
  "'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"),
  stdout = TRUE)
token <- system2("python3", c("-c",
  sprintf("'from cryptography.fernet import Fernet; print(Fernet(b\"%s\").encrypt(b\"SECRET-API-KEY\").decode())'", key)),
  stdout = TRUE)

Sys.setenv(APP_SECRET_KEY = key)
ok("a token encrypted with the right key round-trips",
   identical(.decrypt_key(token), "SECRET-API-KEY"), .decrypt_key(token))

wrong <- system2("python3", c("-c",
  "'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"), stdout = TRUE)
Sys.setenv(APP_SECRET_KEY = wrong)
got <- tryCatch(.decrypt_key(token), error = identity)
ok("a WRONG master key fails visibly", inherits(got, "error") &&
   grepl("Cannot decrypt configured API key", conditionMessage(got), fixed = TRUE))
ok("a wrong key never leaks ciphertext or credentials", inherits(got, "error") &&
   !any(vapply(c(token, key, wrong, "SECRET-API-KEY"), function(value)
     grepl(value, conditionMessage(got), fixed = TRUE), logical(1))))

Sys.setenv(APP_SECRET_KEY = "")
got <- tryCatch(.decrypt_key(token), error = identity)
ok("a MISSING master key reports the required setting", inherits(got, "error") &&
   grepl("APP_SECRET_KEY is missing", conditionMessage(got), fixed = TRUE))
Sys.setenv(APP_SECRET_KEY = key)
ok("an empty token short-circuits to \"\"", identical(.decrypt_key(""), ""))
ok("a garbage token fails visibly", inherits(tryCatch(
   .decrypt_key("not-a-fernet-token"), error = identity), "error"))

cat("\n", if (fail == 0L) "ALL FETCH-PLUMBING CHECKS PASSED" else paste(fail, "FAILURE(S)"), "\n", sep = "")

cat("\n7. fetch_log records the difference (the silently-dead-source guard)\n")
source("R/07_db_questions.R")
tmp <- "dev/state/tmp/p3_fetchlog.sqlite"
unlink(paste0(tmp, c("", "-wal", "-shm")))
con <- DBI::dbConnect(RSQLite::SQLite(), tmp)
db_init_schema(con, "db/schema.sql")
DBI::dbExecute(con, "INSERT INTO questions(id,label) VALUES('q','Q')")
install_mocks(failing = c("openalex"))
out7 <- withCallingHandlers(fetch_question(q5, queries, settings),
                            message = function(m) invokeRestart("muffleMessage"))
log_fetch(con, "q", "lit", attr(out7, "source_counts"))
fl <- DBI::dbGetQuery(con, "SELECT source, n_fetched, ok FROM fetch_log ORDER BY source")
ok("the broken source is logged with ok=0",
   fl$ok[fl$source == "oa"] == 0L, paste(fl$source, fl$ok, collapse=" "))
ok("healthy sources are logged with ok=1",
   all(fl$ok[fl$source != "oa"] == 1L))
ok("a source that legitimately returned nothing is still ok=1",
   { install_mocks(); e <- fetch_question(q5, queries, settings)
     sc2 <- attr(e, "source_counts"); isTRUE(sc2$pubmed$ok) })
DBI::dbDisconnect(con)

cat("\n", if (fail == 0L) "ALL FETCH-PLUMBING CHECKS PASSED" else paste(fail, "FAILURE(S)"), "\n", sep = "")
quit(status = if (fail > 0L) 1L else 0L)
