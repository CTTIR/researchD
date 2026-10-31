# ============================================================
# 04_rank.R
# Max-recall ranking: order, never filter. Score = weighted sum of
# recency, source priority, and term-overlap relevance.
# ============================================================

# Count distinct concept terms appearing in title+abstract.
.relevance_score <- function(recs, q) {
  txt <- tolower(paste(recs$title, recs$abstract))
  all_terms <- unique(unlist(lapply(q$concepts, function(c) c(c$terms, c$mesh))))
  all_terms <- tolower(all_terms)
  all_terms <- all_terms[!is.na(all_terms) & nzchar(trimws(all_terms))]
  vapply(txt, function(t) {
    sum(vapply(all_terms, function(term) grepl(term, t, fixed = TRUE), logical(1)))
  }, numeric(1)) / max(length(all_terms), 1)
}

rank_records <- function(recs, q, settings) {
  if (nrow(recs) == 0) return(recs)
  w <- settings$ranking
  # recency: 0..1 over the lookback window.
  # Some sources legitimately omit a date; if EVERY date is missing, min()
  # warns and returns Inf, so fall back to a neutral span. Records with no
  # date get recency 0 rather than an NA that would poison the score.
  oldest <- suppressWarnings(min(recs$pub_date, na.rm = TRUE))
  span <- if (!is.finite(as.numeric(oldest))) 1
          else max(as.numeric(Sys.Date() - oldest), 1)
  recency <- 1 - (as.numeric(Sys.Date() - recs$pub_date) / span)
  recency[!is.finite(recency)] <- 0
  recency <- pmax(0, pmin(1, recency))
  # source priority normalized
  sp <- vapply(recs$source, function(s) w$source_priority[[s]] %||% 0, numeric(1))
  sp <- sp / max(max(unlist(w$source_priority)), 1)
  # relevance
  rel <- .relevance_score(recs, q)

  recs$score <- w$weight_recency * recency +
                w$weight_source  * sp +
                w$weight_relevance * rel
  recs[order(-recs$score), , drop = FALSE]
}
