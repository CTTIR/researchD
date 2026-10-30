# ============================================================
# 03_dedup.R
# Cross-source dedup. Dedup key: DOI (normalized) if present, else
# exact normalized title+year. Persistent seen-state lives in the `records` table
# and is applied by persist_records() in 07_db_questions.R.
# ============================================================

.norm_doi <- function(x) {
  if (is.null(x)) return("")
  x <- as.character(x); x[is.na(x)] <- ""
  x <- tolower(trimws(x))
  x <- gsub("^https?://(dx\\.)?doi\\.org/", "", x)
  x
}

.norm_title <- function(x) {
  if (is.null(x)) return("")
  x <- as.character(x); x[is.na(x)] <- ""
  x <- tolower(x)
  x <- gsub("[^a-z0-9 ]", " ", x)
  x <- gsub("\\s+", " ", x)
  trimws(x)
}

# Coalesce repeated sightings of the same contributing source record while
# retaining which fields that record supplied. JSON arrays stay arrays even
# when only one contributor or field remains.
.provenance_json <- function(entries) {
  merged <- list()
  for (entry in entries) {
    identity <- list(source = entry$source %||% "", ext_id = entry$ext_id %||% "",
                     doi = entry$doi %||% "")
    key <- as.character(jsonlite::toJSON(identity, auto_unbox = TRUE))
    previous <- merged[[key]]
    fields <- sort(unique(c(unlist(previous$fields, use.names = FALSE),
                            unlist(entry$fields, use.names = FALSE))))
    identity$fields <- as.list(fields)
    merged[[key]] <- identity
  }
  as.character(jsonlite::toJSON(unname(merged), auto_unbox = TRUE))
}

# Collapse duplicates *within* this batch, preferring richer sources.
.collapse_batch <- function(recs, source_priority) {
  if (nrow(recs) == 0L) return(recs)
  recs$.doi_n <- .norm_doi(recs$doi)
  recs$.title_n <- .norm_title(recs$title)
  recs$.year <- format(recs$pub_date, "%Y")
  recs$.year[is.na(recs$.year)] <- ""
  # primary key: doi if present else title+year
  recs$dedup_key <- ifelse(nzchar(recs$.doi_n),
                           paste0("doi:", recs$.doi_n),
                           paste0("ti:", recs$.title_n, "|", recs$.year))
  # Dropping non-ASCII letters would conflate distinct scientific concepts
  # (e.g. alpha and beta receptor symbols). Preserve exact Unicode spelling
  # and case under a separate key; only ASCII whitespace is normalized so the
  # Python ingestion path produces exactly the same key across locales.
  unicode_title <- !is.na(recs$title) & grepl("[^\\x00-\\x7F]", recs$title, perl = TRUE)
  exact_title <- trimws(gsub("[ \t\r\n]+", " ", recs$title), whitespace = " ")
  unicode_key <- !nzchar(recs$.doi_n) & unicode_title
  recs$dedup_key[unicode_key] <- paste0("tiu:", exact_title[unicode_key], "|", recs$.year[unicode_key])
  missing_identity <- !nzchar(recs$.doi_n) & !nzchar(recs$.title_n) & !unicode_title
  if (any(missing_identity)) {
    ids <- recs$ext_id %||% rep("", nrow(recs))
    if (any(is.na(ids[missing_identity]) | !nzchar(ids[missing_identity])))
      stop("A record has neither DOI, usable title, nor external identifier")
    recs$dedup_key[missing_identity] <- paste0("id:", recs$source[missing_identity], ":", ids[missing_identity])
  }
  recs$.prio <- vapply(recs$source, function(s)
    source_priority[[s]] %||% 0, numeric(1))
  recs <- recs[order(-recs$.prio), ]
  merged <- lapply(unique(recs$dedup_key), function(key) {
    copies <- recs[recs$dedup_key == key, , drop = FALSE]
    row <- copies[1L, , drop = FALSE]
    provenance <- list()
    fields <- intersect(c("abstract", "authors", "journal", "url", "oa_url"), names(copies))
    for (i in seq_len(nrow(copies))) {
      filled <- character(0)
      for (field in fields) {
        if ((is.na(row[[field]][1]) || !nzchar(row[[field]][1])) &&
            !is.na(copies[[field]][i]) && nzchar(copies[[field]][i])) {
          row[[field]][1] <- copies[[field]][i]
          filled <- c(filled, field)
        }
      }
      inherited <- if ("provenance" %in% names(copies))
        tryCatch(jsonlite::fromJSON(copies$provenance[i] %||% "[]", simplifyVector = FALSE),
                 error = function(e) list()) else list()
      if (length(inherited)) provenance <- c(provenance, inherited)
      else provenance[[length(provenance) + 1L]] <- list(
        source = copies$source[i], ext_id = copies$ext_id[i] %||% "",
        doi = copies$doi[i] %||% "", fields = as.list(filled))
    }
    row$provenance <- .provenance_json(provenance)
    row
  })
  recs <- do.call(rbind, merged)
  recs[, setdiff(names(recs), c(".doi_n", ".title_n", ".year", ".prio")), drop = FALSE]
}
