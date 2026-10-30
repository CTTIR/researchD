# Independent delivery review: nullable historical records must remain usable.
# All remote responses are synthetic; no Zotero library is contacted.
source("R/00_utils.R")
source("R/03_dedup.R")
source("R/04_rank.R")
source("R/05_deliver.R")
source("R/06_classify.R")
source("R/07_db_questions.R")

testthat::test_that("a legacy missing classification renders its actual record once", {
  record <- data.frame(source = "pubmed", title = "Undated paper", authors = "A",
    pub_date = NA_character_, url = "https://example.invalid/paper", score = 1,
    study_type = NA_character_, is_preprint = 0L)
  batch <- list(list(label = "Q", recs = record))
  html <- render_digest_html(batch, "2026-10-30")
  md <- render_digest_md(batch, "2026-10-30")
  testthat::expect_match(html, "Undated paper", fixed = TRUE)
  testthat::expect_match(md, "Undated paper", fixed = TRUE)
  testthat::expect_length(gregexpr("<li ", html, fixed = TRUE)[[1]], 1L)
  testthat::expect_length(gregexpr("Undated paper", md, fixed = TRUE)[[1]], 1L)
  testthat::expect_match(html, STUDY_TYPE_LABEL[["unclassified"]], fixed = TRUE)
})

testthat::test_that("undated records use Zotero's empty date and leave the queue on success", {
  con <- DBI::dbConnect(RSQLite::SQLite(), ":memory:")
  on.exit(DBI::dbDisconnect(con), add = TRUE)
  db_init_schema(con, "db/schema.sql")
  DBI::dbExecute(con, "INSERT INTO questions(id,label,delivery) VALUES('q','Q','[\"zotero\"]')")
  DBI::dbExecute(con, "INSERT INTO records(dedup_key,question_id,source,title,doi,pub_date)
                      VALUES('id:undated','q','pubmed','Undated paper','',NULL)")
  DBI::dbExecute(con, "INSERT INTO delivery_outbox(channel,question_id,dedup_key)
                      VALUES('zotero','q','id:undated')")
  withr::local_envvar(RESEARCHD_TEST_UNDATED_KEY = "synthetic-key")
  settings <- list(zotero = list(enabled = TRUE, api_key_env = "RESEARCHD_TEST_UNDATED_KEY",
    library_id = "1234", library_type = "user", collection_per_question = FALSE))
  payload <- NULL
  testthat::local_mocked_bindings(req_perform = function(req, ...) {
    payload <<- req$body$data
    httr2::response(status_code = 200L, headers = list(`content-type` = "application/json"),
      body = charToRaw('{"successful":{"0":{"key":"SYNTHKEY"}}}'))
  }, .package = "httr2")
  testthat::expect_message(deliver_zotero(data.frame(), "q", settings, con), "delivered 1")
  testthat::expect_identical(payload[[1L]]$date, "")
  testthat::expect_identical(DBI::dbGetQuery(con, "SELECT state FROM delivery_outbox")$state, "sent")
})

testthat::test_that("unclassified ingested preprints still receive flags and canonical ranking", {
  con <- DBI::dbConnect(RSQLite::SQLite(), ":memory:")
  on.exit(DBI::dbDisconnect(con), add = TRUE)
  db_init_schema(con, "db/schema.sql")
  DBI::dbExecute(con, "INSERT INTO questions(id,label) VALUES('q','Q')")
  DBI::dbExecute(con, "INSERT INTO records(dedup_key,question_id,source,title,journal,pub_date,
                      study_type,is_preprint,score) VALUES('x','q','ingest','Alpha research',
                      'bioRxiv','historic invalid date','unclassified',0,0)")
  question <- list(concepts = list(list(terms = "alpha")))
  settings <- list(ranking = list(weight_recency = 1, weight_source = 0.5,
    weight_relevance = 2, source_priority = list(ingest = 1)))
  testthat::expect_equal(reclassify_unclassified(con, "q", question, settings), 1L)
  row <- DBI::dbGetQuery(con, "SELECT study_type,is_preprint,score FROM records")
  testthat::expect_identical(row$study_type, "unclassified")
  testthat::expect_identical(row$is_preprint, 1L)
  testthat::expect_gt(row$score, 0)
  testthat::expect_true(is.finite(row$score))
})
