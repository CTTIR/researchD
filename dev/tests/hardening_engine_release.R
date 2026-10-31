# Release regressions: configuration imports, ranking and incomplete retrieval.
# Network responses below are synthetic and use no provider credentials.
source('R/00_utils.R')
source('R/01_query_builder.R')
source('R/02_fetch.R')
source('R/03_dedup.R')
source('R/04_rank.R')
source('R/07_db_questions.R')
source('R/08_signals.R')
source('R/09_signals_persist.R')
library(testthat)

memory_db <- function() {
  con <- DBI::dbConnect(RSQLite::SQLite(), ':memory:')
  db_init_schema(con, 'db/schema.sql')
  con
}

seed_question <- function(id = 'q') list(id = id, label = 'Question',
  concepts = list(list(name = 'topic', terms = 'alpha', mesh = character())),
  enabled = FALSE, sources = list(pubmed = FALSE, europepmc = TRUE),
  delivery = list(), study_filter = 'Clinical Trial', from_days_back = 14L,
  digest_mode = 'threshold', min_score = 1.5, email_enabled = FALSE,
  email_to = 'researcher@example.invalid')

test_that('MeSH-only concepts remain required on sources without a MeSH field', {
  q <- list(concepts = list(list(terms = character(), mesh = 'Spinal Cord Injuries'),
                           list(terms = 'rehabilitation', mesh = character())))
  expect_identical(build_clinicaltrials(q), '("Spinal Cord Injuries") AND (rehabilitation)')
  expect_identical(build_crossref(q), 'Spinal Cord Injuries rehabilitation')
  matches <- build_biorxiv_matcher(q)
  expect_true(matches('Rehabilitation following spinal cord injuries'))
  expect_false(matches('Rehabilitation following unrelated injuries'))
  expect_false(matches('Spinal cord injuries and inflammation'))
})

test_that('YAML seed preserves controls and serializes singletons as arrays', {
  con <- memory_db(); on.exit(DBI::dbDisconnect(con), add = TRUE)
  path <- tempfile(fileext = '.yaml'); on.exit(unlink(path), add = TRUE)
  yaml::write_yaml(list(questions = list(seed_question())), path)
  suppressMessages(seed_questions_from_yaml(con, path))
  row <- DBI::dbGetQuery(con, 'SELECT * FROM questions')
  expect_identical(row$enabled, 0L)
  expect_identical(row$delivery, '[]')
  expect_identical(row$study_filter, '["Clinical Trial"]')
  expect_identical(row$email_to, '["researcher@example.invalid"]')
  expect_identical(jsonlite::fromJSON(row$sources), list(pubmed = FALSE, europepmc = TRUE))
  expect_identical(row$digest_mode, 'threshold')
  expect_equal(row$min_score, 1.5)
  expect_identical(DBI::dbGetQuery(con, 'SELECT terms FROM concepts')$terms, '["alpha"]')
  expect_length(load_questions_db(con), 0L)
  DBI::dbExecute(con, 'UPDATE questions SET enabled=1')
  loaded <- load_questions_db(con)[[1L]]
  expect_identical(loaded$sources, list(pubmed = FALSE, europepmc = TRUE))
  expect_identical(loaded$concepts[[1L]]$terms, 'alpha')
  # Seeding again must neither change user edits nor leave a transaction open.
  seed_questions_from_yaml(con, path)
  DBI::dbExecute(con, 'BEGIN IMMEDIATE'); DBI::dbRollback(con)
  expect_identical(DBI::dbGetQuery(con, 'SELECT enabled FROM questions')$enabled, 1L)
})

test_that('a later malformed seed rolls back earlier questions and concepts', {
  con <- memory_db(); on.exit(DBI::dbDisconnect(con), add = TRUE)
  path <- tempfile(fileext = '.yaml'); on.exit(unlink(path), add = TRUE)
  broken <- seed_question('broken'); broken$concepts[[1L]]$name <- NULL
  yaml::write_yaml(list(questions = list(seed_question(), broken)), path)
  expect_error(seed_questions_from_yaml(con, path), 'concept name')
  expect_equal(DBI::dbGetQuery(con, 'SELECT COUNT(*) n FROM questions')$n, 0L)
  expect_equal(DBI::dbGetQuery(con, 'SELECT COUNT(*) n FROM concepts')$n, 0L)
  yaml::write_yaml(list(questions = list()), path)
  seed_questions_from_yaml(con, path)
  expect_equal(DBI::dbGetQuery(con, 'SELECT COUNT(*) n FROM questions')$n, 0L)
  legacy <- seed_question(); legacy$delivery <- c('file', 'email', 'zotero')
  legacy$email_enabled <- TRUE
  yaml::write_yaml(list(questions = list(legacy)), path)
  suppressMessages(seed_questions_from_yaml(con, path))
  row <- DBI::dbGetQuery(con, 'SELECT delivery,email_enabled FROM questions')
  expect_identical(jsonlite::fromJSON(row$delivery), c('file', 'email', 'zotero'))
  expect_identical(row$email_enabled, 1L)
})

test_that('malformed source controls fail instead of masquerading as quiet sources', {
  con <- memory_db(); on.exit(DBI::dbDisconnect(con), add = TRUE)
  DBI::dbExecute(con, "INSERT INTO questions(id,label,sources) VALUES('q','Q','{\"pubmed\":\"false\"}')")
  loaded <- suppressMessages(load_questions_db(con))
  expect_length(loaded, 0L)
  expect_named(attr(loaded, 'load_errors'), 'q')
  expect_match(attr(loaded, 'load_errors')$q, 'Invalid logical object')
  DBI::dbExecute(con, "INSERT INTO signal_config(question_id) VALUES('q')")
  for (field in c('sources', 'feed_categories', 'scrape_targets', 'api_keys_enc')) {
    DBI::dbExecute(con, paste0('UPDATE signal_config SET ', field, '=?'), params = list('{private-canary'))
    error <- tryCatch(load_signal_config(con, 'q'), error = identity)
    expect_s3_class(error, 'error')
    expect_match(conditionMessage(error), paste0('signal_config.', field), fixed = TRUE)
    expect_false(grepl('private-canary', conditionMessage(error), fixed = TRUE))
    DBI::dbExecute(con, paste0('UPDATE signal_config SET ', field, '=?'),
                   params = list(if (field == 'scrape_targets') '[]' else '{}'))
  }
  DBI::dbExecute(con, "UPDATE signal_config SET sources='{\"openfda\":\"true\"}'")
  expect_error(load_signal_config(con, 'q'), 'Invalid logical object')
  DBI::dbExecute(con, "UPDATE signal_config SET sources='{\"typo_source\":true}'")
  expect_error(load_signal_config(con, 'q'), 'Invalid logical object')
})

test_that('future dates and empty relevance terms cannot manufacture oversized scores', {
  recs <- data.frame(title = c('Alpha', 'Future'), abstract = '', source = 'pubmed',
                     pub_date = Sys.Date() + c(0, 365))
  q <- list(concepts = list(list(terms = c('', NA_character_, 'alpha'))))
  settings <- list(ranking = list(weight_recency = 1, weight_source = 0,
                                  weight_relevance = 0, source_priority = list(pubmed = 1)))
  ranked <- rank_records(recs, q, settings)
  expect_true(all(is.finite(ranked$score)))
  expect_true(all(ranked$score >= 0 & ranked$score <= 1))
  expect_equal(unname(.relevance_score(recs, q)), c(1, 0))
  expect_true(all(is.na(.parse_date(c('2026-10-31junk', '2026-10-31T99:99:99Z',
                                     '20260230', '2026-10/31', '2026/10-31')))))
  expect_equal(as.character(.parse_date(c('2026-10-31T12:30:00Z', '2026-10', '20261031'))),
               c('2026-10-31', '2026-10-01', '2026-10-31'))
})

test_that('upstream failure details redact configured keys and credential URLs', {
  key <- 'synthetic key/+secret'
  message <- paste('upstream rejected', key, utils::URLencode(key, reserved = TRUE),
                   'https://example.invalid?api_key=url-secret&search=alpha',
                   'https://user:password@example.invalid')
  out <- NULL
  log <- capture.output(out <- .safe(stop(message), 'provider', secrets = key), type = 'message')
  detail <- paste(log, attr(out, 'fetch_error'))
  expect_false(any(vapply(c(key, utils::URLencode(key, reserved = TRUE), 'url-secret', 'password'),
                         function(secret) grepl(secret, detail, fixed = TRUE), logical(1))))
  expect_match(detail, 'upstream rejected')
  expect_match(detail, '[redacted]', fixed = TRUE)
  expect_false(.fetch_health(out)$ok)
})

test_that('premature empty pages retain failure status even without any rows', {
  settings <- list(fetch = list(max_records = 5), openalex = list(api_key = 'synthetic'))
  q <- list(concepts = list(list(terms = 'alpha')))
  local_mocked_bindings(req_perform = function(req, ...) req,
    resp_status = function(...) 200L,
    resp_body_json = function(req, ...) {
      if (grepl('clinicaltrials', req$url)) return(list(studies = list(), nextPageToken = 'more'))
      if (grepl('openalex', req$url)) return(list(results = list(), meta = list(count = 3L)))
      if (grepl('crossref', req$url)) return(list(message = list(items = list(), `total-results` = 3L)))
      if (grepl('semanticscholar', req$url)) return(list(data = list(), total = 3L))
      list(results = list(), meta = list(results = list(total = 3L)))
    }, .package = 'httr2')
  fetched <- list(fetch_clinicaltrials('alpha', 7, settings),
    .ctgov_signals(.fetch_ctgov_studies('alpha', 7, settings)),
    fetch_openalex(list('alpha'), 7, settings), fetch_crossref('alpha', 7, settings),
    fetch_semanticscholar(list('alpha'), 7, settings), fetch_openfda(q, 7, settings))
  expect_true(all(vapply(fetched, function(x) nrow(x) == 0L, logical(1))))
  expect_true(all(vapply(fetched, function(x) !.fetch_health(x)$ok, logical(1))))
})

test_that('later malformed pages preserve already retrieved evidence and mark degradation', {
  settings <- list(fetch = list(max_records = 5), openalex = list(api_key = 'synthetic'))
  q <- list(concepts = list(list(terms = 'alpha')))
  calls <- new.env(parent = emptyenv())
  today <- as.character(Sys.Date())
  local_mocked_bindings(req_perform = function(req, ...) req,
    resp_status = function(...) 200L,
    resp_body_json = function(req, ...) {
      host <- httr2::url_parse(req$url)$hostname
      calls[[host]] <- (calls[[host]] %||% 0L) + 1L
      if (calls[[host]] > 1L) stop('synthetic malformed upstream JSON')
      if (grepl('clinicaltrials', host)) return(list(studies = list(list(protocolSection = list(
        identificationModule = list(nctId = 'NCT00000001', briefTitle = 'First trial'),
        statusModule = list(lastUpdatePostDateStruct = list(date = today))))), nextPageToken = 'more'))
      if (grepl('openalex', host)) return(list(results = list(list(id = 'W1', title = 'First paper',
        publication_date = today)), meta = list(count = 2L, next_cursor = 'more')))
      if (grepl('crossref', host)) return(list(message = list(items = list(list(DOI = '10.1/first',
        title = list('First paper'))), `total-results` = 2L, `next-cursor` = 'more')))
      if (grepl('semanticscholar', host)) return(list(data = list(list(paperId = 'S1',
        title = 'First paper', publicationDate = today)), total = 2L, `next` = 1L))
      list(results = list(list(set_id = 'label-1', effective_time = gsub('-', '', today))),
           meta = list(results = list(total = 2L)))
    }, .package = 'httr2')
  fetched <- list(fetch_clinicaltrials('alpha', 7, settings),
    fetch_openalex(list('alpha'), 7, settings), fetch_crossref('alpha', 7, settings),
    fetch_semanticscholar(list('alpha'), 7, settings), fetch_openfda(q, 7, settings))
  expect_true(all(vapply(fetched, function(x) nrow(x) == 1L, logical(1))))
  expect_true(all(vapply(fetched, function(x) !.fetch_health(x)$ok, logical(1))))
  expect_true(all(unlist(as.list(calls)) == 2L))
})
