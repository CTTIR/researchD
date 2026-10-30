# ============================================================
# 06_classify.R
# Two-axis classification. PREFERS authoritative publication-type
# tags (PubMed [pt] / Europe PMC pubType — both MeSH-derived) and
# falls back to title heuristics ONLY when no usable tag exists.
#
#   study_type  (one of, evidence-ordered):
#     systematic_review | rct | clinical_trial | guideline |
#     observational | case_report | review | other | unclassified
#
#   is_preprint (0/1)  -- orthogonal status flag, NOT a study type.
#     An RCT released as a preprint is study_type="rct", is_preprint=1.
#
# Ordering is deliberate (load-bearing across this file, web/app.py,
# web/scheduler.py):
#   systematic_review before rct (a "systematic review of RCTs" is SR)
#   rct before clinical_trial (RCT is the higher-evidence subtype)
#   review AFTER systematic_review (narrative review is the fallback)
#   unclassified is the recall safety net for records with no tag AND
#   no title signal (common for brand-new, not-yet-indexed items).
# ============================================================

# ---- Authoritative tag vocabulary ----------------------------
# Maps the EXACT publication-type strings used by PubMed/Europe PMC
# to our buckets. These are checked in evidence order; the FIRST
# matching tag in a record's tag set wins (so a record tagged both
# "Meta-Analysis" and "Journal Article" classifies as systematic_review).
#
# Source: PubMed publication types (MeSH). Europe PMC reuses most of
# the same strings in its pubType field.
.TAG_TO_BUCKET <- list(
  systematic_review = c("systematic review", "meta-analysis",
                        "systematic review and meta-analysis"),
  rct               = c("randomized controlled trial"),
  clinical_trial    = c("clinical trial", "clinical trial, phase i",
                        "clinical trial, phase ii", "clinical trial, phase iii",
                        "clinical trial, phase iv", "clinical trial protocol",
                        "controlled trial", "controlled clinical trial",
                        "pragmatic clinical trial", "equivalence trial",
                        "adaptive clinical trial"),
  guideline         = c("guideline", "practice guideline",
                        "consensus development conference",
                        "consensus development conference, nih"),
  case_report       = c("case reports"),
  observational     = c("observational study", "comparative study",
                        "observational study, veterinary",
                        "twin study", "validation study", "evaluation study"),
  review            = c("review", "scoping review", "narrative review"),
  other             = c("editorial", "letter", "comment", "news",
                        "published erratum", "retraction of publication",
                        "expression of concern", "biography", "interview",
                        "historical article", "portrait", "address",
                        "congress", "lecture", "introductory journal article",
                        "personal narrative", "autobiography")
)
# Order in which buckets are tested against the tag set.
.BUCKET_ORDER <- c("systematic_review", "rct", "clinical_trial",
                   "guideline", "case_report", "observational",
                   "review", "other")

# Classify from a set of authoritative tags. Returns a bucket or "".
.classify_from_tags <- function(tags) {
  if (length(tags) == 0) return("")
  tags <- tolower(trimws(tags))
  tags <- tags[nzchar(tags)]
  if (length(tags) == 0) return("")
  for (bucket in .BUCKET_ORDER) {
    if (any(tags %in% .TAG_TO_BUCKET[[bucket]])) return(bucket)
  }
  # A record carrying ONLY "journal article" (or similar generic tags) has a
  # real tag but no design signal. Treat as a weak signal, not a hard answer,
  # so the title heuristic gets a chance; if that also fails -> unclassified.
  if (any(tags %in% c("journal article", "research support, non-u.s. gov't",
                      "research support, u.s. gov't"))) return("generic")
  ""
}

# ---- Title/abstract heuristic fallback -----------------------
# Only used when tags are absent or generic. Deliberately broad
# because preprints/unindexed records lean entirely on phrasing.
.classify_from_text <- function(source, title) {
  s  <- tolower(source %||% "")
  ti <- tolower(title %||% "")

  if (grepl("systematic review|meta-analysis|meta analysis|metaanalysis|cochrane", ti))
    return("systematic_review")
  if (!grepl("non[- ]?randomi[sz]ed|not randomi[sz]ed", ti) &&
      grepl("randomi[sz]ed controlled trial|randomi[sz]ed clinical trial|randomi[sz]ed trial|randomi[sz]ed study|randomi[sz]ed,? (double|single|open|placebo)|\\brct\\b|random allocation|randomly assigned", ti))
    return("rct")
  if (grepl("clinical trial|\\btrial\\b|phase (i{1,3}v?|[1-4])\\b|interventional|single-arm|open-label", ti))
    return("clinical_trial")
  if (grepl("guideline|practice guideline|consensus|recommendation statement|position statement", ti))
    return("guideline")
  if (grepl("case report|case series|case study", ti))
    return("case_report")
  if (grepl("cohort|case-control|case control|cross-sectional|cross sectional|observational|comparative study|registry|retrospective|prospective study|longitudinal", ti))
    return("observational")
  if (grepl("\\breview\\b|scoping review|narrative review|literature review|overview", ti))
    return("review")
  # Word-bounded: unbounded "note"/"letter"/"news" swallow ordinary research
  # titles ("denoted pathways...", "footnotes to...", "newsletter of...") and
  # quietly demote real papers into the lowest-priority bucket.
  if (grepl(paste0("editorial|\\bletters?\\b|\\bcomment(ary|aries|s)?\\b|",
                   "\\bnews(letter)?\\b|erratum|\\bcorrection\\b|retraction|",
                   "\\bnotes?\\b|biography|interview|congress"), ti))
    return("other")
  ""
}

# ---- Preprint detection (orthogonal) -------------------------
.detect_preprint <- function(source, tags, journal) {
  s <- tolower(source %||% "")
  blob <- tolower(paste(paste(tags, collapse = " "), journal %||% ""))
  s %in% c("biorxiv", "medrxiv") ||
    grepl("preprint|biorxiv|medrxiv|research square|ssrn|arxiv|preprints\\.org", blob)
}

# ---- Main per-record dispatcher ------------------------------
# `tags` is a character vector of authoritative publication types
# (may be empty). `type_str` is the legacy flattened string kept for
# back-compat with sources that only provide a single string.
.detect_study_type <- function(source, tags, type_str, title) {
  s <- tolower(source %||% "")

  # ClinicalTrials.gov carries its own study-type field via type_str.
  t <- tolower(type_str %||% "")
  if (s == "clinicaltrials" || startsWith(t, "trial:")) {
    if (grepl("observational", paste(t, tolower(title %||% "")))) return("observational")
    return("clinical_trial")
  }

  # 1. Trust authoritative tags first.
  by_tag <- .classify_from_tags(tags)
  if (nzchar(by_tag) && by_tag != "generic") return(by_tag)

  # 2. Fall back to title heuristic (also covers the "generic" case).
  by_text <- .classify_from_text(s, title)
  if (nzchar(by_text)) return(by_text)

  # A generic Journal Article tag does not establish a study design (it can
  # describe laboratory, animal, methods, or clinical research). Preserve
  # that uncertainty instead of manufacturing an observational classification.
  "unclassified"
}

# ---- Vectorized entry point ----------------------------------
# Sources that populate `type` with authoritative MeSH publication types.
# For these, we split `type` on "; " into structured tags and trust them.
# Other sources' `type` strings are not clean tag sets -> title heuristic.
.TAG_SOURCES <- c("pubmed", "europepmc")

classify_study_type <- function(recs) {
  if (nrow(recs) == 0) {
    recs$study_type <- character(0); recs$is_preprint <- integer(0); return(recs)
  }
  st <- character(nrow(recs)); pp <- integer(nrow(recs))
  for (i in seq_len(nrow(recs))) {
    src <- tolower(recs$source[i] %||% "")
    tags <- if (src %in% .TAG_SOURCES) {
      # split "Journal Article; Review; Meta-Analysis" -> structured tags
      unique(trimws(strsplit(recs$type[i] %||% "", "[;,]")[[1]]))
    } else {
      character(0)   # non-tag source: force title heuristic
    }
    st[i] <- .detect_study_type(recs$source[i], tags, recs$type[i], recs$title[i])
    pp[i] <- as.integer(.detect_preprint(recs$source[i], tags, recs$journal[i]))
  }
  recs$study_type <- st
  recs$is_preprint <- pp
  recs
}

# Display order + labels for email sections and feed categories.
STUDY_TYPE_ORDER <- c(
  "systematic_review", "rct", "clinical_trial", "guideline",
  "observational", "case_report", "review", "other", "unclassified"
)
STUDY_TYPE_LABEL <- c(
  systematic_review = "Systematic reviews & meta-analyses",
  rct               = "Randomized controlled trials",
  clinical_trial    = "Clinical trials (other)",
  guideline         = "Guidelines & consensus",
  observational     = "Observational & comparative studies",
  case_report       = "Case reports",
  review            = "Reviews (narrative)",
  other             = "Editorials, letters & news",
  unclassified      = "Unclassified / not yet indexed"
)
