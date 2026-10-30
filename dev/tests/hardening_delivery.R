source('R/00_utils.R')
source('R/03_dedup.R')
source('R/06_classify.R')
source('R/05_deliver.R')
source('R/07_db_questions.R')
library(testthat)

response <- function(status, body) httr2::response(status_code=status,
  headers=list(`content-type`='application/json'),
  body=charToRaw(as.character(jsonlite::toJSON(body,auto_unbox=TRUE))))

test_that('item-level rejection stays queued and succeeds on a later no-new-record run', {
  con <- DBI::dbConnect(RSQLite::SQLite(), ':memory:')
  on.exit(DBI::dbDisconnect(con),add=TRUE)
  db_init_schema(con,'db/schema.sql')
  DBI::dbExecute(con,"INSERT INTO questions(id,label,delivery) VALUES('q','Q','[\"zotero\"]')")
  rec <- data.frame(source='pubmed',ext_id='1',doi='10.1/synthetic',title='Synthetic',
    abstract='',authors='Heller R; Other A',journal='J',pub_date=as.Date('2026-10-30'),
    url='https://example.invalid/paper',type='Journal Article',study_type='unclassified',
    is_preprint=0L,score=1)
  persist_records(con,rec,'q',list(pubmed=2))
  Sys.setenv(RESEARCHD_TEST_ZOTERO_KEY='synthetic-key')
  on.exit(Sys.unsetenv('RESEARCHD_TEST_ZOTERO_KEY'),add=TRUE)
  settings <- list(zotero=list(enabled=TRUE,api_key_env='RESEARCHD_TEST_ZOTERO_KEY',
     library_id='1234',library_type='user',collection_per_question=FALSE))
  calls <- list(); reject <- TRUE; reconcile <- FALSE
  local_mocked_bindings(req_perform=function(req, ...) {
    calls[[length(calls)+1L]] <<- req
    if (is.null(req$body)) {
      if (reconcile) return(response(200L,list(data=list(title='Synthetic',DOI='10.1/synthetic'))))
      return(response(404L,list(error='not yet created')))
    }
    if (reject) return(response(200L,list(failed=list('0'=list(code=400L,message='invalid item')))))
    response(200L,list(successful=list('0'=list(key='SYNTHKEY'))))
  },.package='httr2')
  expect_error(deliver_zotero(data.frame(),'q',settings,con),'remain pending')
  state <- DBI::dbGetQuery(con,'SELECT * FROM delivery_outbox')
  expect_identical(state$state,'pending')
  expect_match(state$last_error,'did not accept')
  expect_true(nzchar(state$remote_key))
  reject <- FALSE
  expect_message(deliver_zotero(data.frame(),'q',settings,con),'delivered 1')
  state2 <- DBI::dbGetQuery(con,'SELECT * FROM delivery_outbox')
  expect_identical(state2$state,'sent')
  expect_identical(state2$remote_key,state$remote_key)
  expect_identical(state2$attempts,2L)
  payload <- calls[[length(calls)]]$body$data
  expect_length(payload[[1]]$creators,2L)
  expect_identical(payload[[1]]$key,state$remote_key)
  # An acknowledged remote item with an unacknowledged local write reconciles
  # by stable identity, without creating a second remote item.
  DBI::dbExecute(con,"UPDATE delivery_outbox SET state='pending'")
  reconcile <- TRUE; calls <- list()
  expect_message(deliver_zotero(data.frame(),'q',settings,con),'delivered 1')
  expect_length(calls,1L)
  expect_null(calls[[1]]$body)
  DBI::dbExecute(con,"UPDATE delivery_outbox SET state='pending',destination='https://api.zotero.org/users/999'")
  expect_error(deliver_zotero(data.frame(),'q',settings,con),'destination differs')
  expect_identical(DBI::dbGetQuery(con,'SELECT state FROM delivery_outbox')$state,'pending')
})

test_that('file digest links cannot inject HTML attributes or script schemes', {
  expect_identical(.delivery_url('javascript:alert(1)'), '')
  escaped <- htmltools::htmlEscape(.delivery_url("https://example.invalid/a'b"),attribute=TRUE)
  expect_false(grepl("'",escaped,fixed=TRUE))
})

test_that('failed file export remains queued, retries, and never overwrites an earlier digest', {
  con <- DBI::dbConnect(RSQLite::SQLite(), ':memory:')
  on.exit(DBI::dbDisconnect(con),add=TRUE)
  db_init_schema(con,'db/schema.sql')
  DBI::dbExecute(con,"INSERT INTO questions(id,label,delivery) VALUES('q','Q','[\"file\"]')")
  rec <- data.frame(source='ingest',ext_id='1',doi='10.1/file',title='<script>bad</script>',
    abstract='',authors='A',journal='J',pub_date=as.Date('2026-10-30'),
    url='javascript:alert(1)',type='',study_type='custom<category>',is_preprint=0L,score=1)
  persist_records(con,rec,'q',list(ingest=1))
  questions <- list(list(id='q',label='<b>Q</b>',delivery='file'))
  directory <- tempfile();dir.create(directory)
  on.exit(unlink(directory,recursive=TRUE),add=TRUE)
  settings <- list(paths=list(digests=directory))
  blocked <- file.path(directory,'regular-file'); writeLines('not a directory',blocked)
  bad_settings <- list(paths=list(digests=blocked))
  expect_error(suppressWarnings(deliver_pending_files(con,questions,bad_settings,'2026-10-30')))
  expect_identical(DBI::dbGetQuery(con,'SELECT state FROM delivery_outbox')$state,'pending')
  first <- suppressMessages(deliver_pending_files(con,questions,settings,'2026-10-30'))
  expect_true(file.exists(first))
  html <- paste(readLines(first),collapse='\n')
  expect_match(html,'custom&lt;category&gt;',fixed=TRUE)
  expect_false(grepl('<script>',html,fixed=TRUE))
  expect_false(grepl('javascript:',html,fixed=TRUE))
  before <- readLines(first)
  rec$doi <- '10.1/second'; rec$title <- 'Second record'
  persist_records(con,rec,'q',list(ingest=1))
  second <- suppressMessages(deliver_pending_files(con,questions,settings,'2026-10-30'))
  expect_false(identical(first,second))
  expect_identical(readLines(first),before)
  expect_true(all(DBI::dbGetQuery(con,'SELECT state FROM delivery_outbox')$state == 'sent'))
})
