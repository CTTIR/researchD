# ============================================================
# 01_query_builder.R
# Translate one structured question (concepts + terms + mesh)
# into the native query syntax of each source.
# ============================================================

# --- helpers ---------------------------------------------------

# Wrap a phrase in quotes if it contains a space (for exact-phrase matching)
.quote_if_phrase <- function(x) {
  ifelse(grepl("\\s", x), paste0('"', x, '"'), x)
}

# OR-join a character vector inside parentheses
.or_join <- function(terms, sep = " OR ") {
  terms <- terms[nzchar(terms)]
  if (length(terms) == 0) return("")
  paste0("(", paste(terms, collapse = sep), ")")
}

# --- PubMed (E-utilities) -------------------------------------
# Free text searched in [tiab] (title/abstract); MeSH in [MeSH Terms].
build_pubmed <- function(q, from_days_back = NULL) {
  concept_blocks <- vapply(q$concepts, function(con) {
    terms <- con$terms %||% character(0)
    terms <- terms[!is.na(terms) & nzchar(trimws(terms))]
    tiab <- if (length(terms)) paste0(.quote_if_phrase(terms), "[tiab]") else character(0)
    mesh <- if (!is.null(con$mesh) && length(con$mesh))
      paste0(.quote_if_phrase(con$mesh), "[MeSH Terms]") else character(0)
    .or_join(c(tiab, mesh))
  }, character(1))
  query <- paste(concept_blocks[nzchar(concept_blocks)], collapse = " AND ")

  if (length(q$study_filter)) {
    pts <- .or_join(paste0(.quote_if_phrase(q$study_filter), "[Publication Type]"))
    query <- paste(query, "AND", pts)
  }
  # Date filter applied at fetch time via reldate, not in the string.
  query
}

# --- Europe PMC -----------------------------------------------
# Uses field tags like TITLE_ABS: and a unified query grammar.
build_europepmc <- function(q) {
  concept_blocks <- vapply(q$concepts, function(con) {
    all_terms <- unique(c(con$terms, con$mesh))
    tagged <- paste0("(TITLE_ABS:", .quote_if_phrase(all_terms), ")")
    .or_join(tagged)
  }, character(1))
  paste(concept_blocks, collapse = " AND ")
}

# --- OpenAlex --------------------------------------------------
# openalexR uses a search string; we OR within concept, but OpenAlex
# `search` is ranked full-text. We build a plain term query and rely
# on its relevance, then AND-filter client-side on concept presence.
build_openalex <- function(q) {
  # Return the concept structure; fetch layer does per-concept search + intersect.
  lapply(q$concepts, function(con) unique(c(con$terms, con$mesh)))
}

# --- ClinicalTrials.gov v2 ------------------------------------
# v2 API: query.term with Essie expression syntax (AND/OR supported).
build_clinicaltrials <- function(q) {
  concept_blocks <- vapply(q$concepts, function(con) {
    all_terms <- unique(c(con$terms))  # mesh headings less useful here
    .or_join(.quote_if_phrase(all_terms))
  }, character(1))
  paste(concept_blocks, collapse = " AND ")
}

# --- bioRxiv / medRxiv ----------------------------------------
# Their API has no full query grammar; we fetch a recent window and
# filter client-side on concept term presence in title/abstract.
build_biorxiv_matcher <- function(q) {
  # Returns a function: given title+abstract text, TRUE if all concepts present.
  concept_patterns <- lapply(q$concepts, function(con) {
    terms <- unique(c(con$terms))
    paste0("\\b", gsub("([.|()\\^{}+$*?\\[\\]])", "\\\\\\1", terms), "\\b",
           collapse = "|")
  })
  function(text) {
    text <- tolower(text)
    all(vapply(concept_patterns, function(p)
      grepl(tolower(p), text, perl = TRUE), logical(1)))
  }
}

# --- Semantic Scholar -----------------------------------------
# Free API (api.semanticscholar.org). Bulk/relevance search over
# title+abstract. Like OpenAlex, we search per concept and intersect
# client-side so every concept must be represented.
build_semanticscholar <- function(q) {
  lapply(q$concepts, function(con) unique(c(con$terms, con$mesh)))
}

# --- Crossref --------------------------------------------------
# Free REST API. Useful because Crossref often has a DOI's metadata
# (and thus the record) BEFORE PubMed finishes MeSH-indexing it, so it
# helps catch the very newest journal articles. Query is a plain string.
build_crossref <- function(q) {
  # one bibliographic query string; relevance-ranked, then concept-filtered
  paste(unlist(lapply(q$concepts, function(con) con$terms)), collapse = " ")
}

# --- master dispatcher ----------------------------------------
build_all_queries <- function(q) {
  list(
    pubmed           = build_pubmed(q),
    europepmc        = build_europepmc(q),
    openalex         = build_openalex(q),
    semanticscholar  = build_semanticscholar(q),
    crossref         = build_crossref(q),
    clinicaltrials   = build_clinicaltrials(q),
    biorxiv_match    = build_biorxiv_matcher(q)
  )
}
