# ============================================================
# Phase 3 — classifier edge-case table (R/06_classify.R).
# Each row: source, type string, journal, title -> expected bucket + preprint.
# Ordering guards (SR before RCT, RCT before clinical_trial, narrative review
# last) are the point of this table, not incidental cases.
# ============================================================
source("R/00_utils.R")
source("R/06_classify.R")

cases <- list(
  # --- authoritative tags (PubMed / Europe PMC) ---
  list("pubmed", "Journal Article; Meta-Analysis", "Lancet",
       "Semaglutide in MASH", "systematic_review", 0,
       "meta-analysis tag beats a plain title"),
  list("pubmed", "Journal Article; Systematic Review; Randomized Controlled Trial", "BMJ",
       "A systematic review of randomized trials of GLP-1", "systematic_review", 0,
       "SR is tested before RCT (load-bearing order)"),
  list("pubmed", "Randomized Controlled Trial; Journal Article", "NEJM",
       "Tirzepatide versus placebo", "rct", 0, "RCT tag"),
  list("pubmed", "Clinical Trial, Phase III", "NEJM",
       "Phase 3 study of resmetirom", "clinical_trial", 0,
       "comma inside a single PubMed tag still resolves"),
  list("pubmed", "Randomized Controlled Trial; Clinical Trial, Phase III", "NEJM",
       "Phase 3 randomized study", "rct", 0, "RCT beats clinical_trial"),
  list("pubmed", "Practice Guideline", "Hepatology",
       "AASLD practice guidance on MASLD", "guideline", 0, "guideline tag"),
  list("pubmed", "Case Reports", "BMJ Case Rep",
       "Severe hepatotoxicity after therapy", "case_report", 0, "case report tag"),
  list("pubmed", "Observational Study", "Gut",
       "Liver outcomes in a national cohort", "observational", 0, "observational tag"),
  list("pubmed", "Review; Journal Article", "Nat Rev Gastroenterol",
       "MASH pathophysiology", "review", 0, "narrative review tag"),
  list("pubmed", "Editorial", "Lancet",
       "The MASH decade", "other", 0, "editorial tag"),
  list("pubmed", "Journal Article", "Hepatology",
       "Hepatic fat fraction and fibrosis markers", "unclassified", 0,
       "generic tag alone does not establish a study design"),
  list("pubmed", "Journal Article; Research Support, Non-U.S. Gov't", "Hepatology",
       "Serum proteomics in steatohepatitis", "unclassified", 0,
       "generic tags only"),

  # --- title heuristic (no usable tag) ---
  list("openalex", "", "J Hepatol",
       "A systematic review and meta-analysis of GLP-1 agonists", "systematic_review", 0,
       "title SR"),
  list("openalex", "", "J Hepatol",
       "A randomised, double-blind, placebo-controlled trial", "rct", 0, "title RCT"),
  list("openalex", "", "J Hepatol",
       "An open-label phase 2 trial of a FGF21 analogue", "clinical_trial", 0,
       "title clinical trial"),
  list("openalex", "", "J Hepatol",
       "Consensus statement on MASLD nomenclature", "guideline", 0, "title guideline"),
  list("openalex", "", "J Hepatol",
       "Case report: acute liver failure", "case_report", 0, "title case report"),
  list("openalex", "", "J Hepatol",
       "Retrospective cohort of 12000 patients", "observational", 0, "title observational"),
  list("openalex", "", "J Hepatol",
       "A narrative review of MASH therapeutics", "review", 0, "title review"),
  list("openalex", "", "J Hepatol",
       "Erratum: dosing table", "other", 0, "title erratum"),
  list("crossref", "journal-article", "J Hepatol",
       "Hepatic stellate cell dynamics", "unclassified", 0,
       "no tag, no title signal -> recall safety net"),

  # --- preprints (orthogonal flag) ---
  list("biorxiv", "Preprint", "bioRxiv",
       "A randomized controlled trial of semaglutide", "rct", 1,
       "preprint RCT: bucket AND flag"),
  list("medrxiv", "Preprint", "medRxiv",
       "Hepatic outcomes in a prospective cohort", "observational", 1,
       "preprint observational"),
  list("medrxiv", "Preprint", "medRxiv",
       "Hepatic stellate cell dynamics in fibrosis", "unclassified", 1,
       "preprint with no signal stays unclassified but flagged"),

  # --- regression: unbounded "note"/"letter"/"news" used to swallow these ---
  list("crossref", "", "J Hepatol",
       "Denoted pathways in hepatic fibrosis", "unclassified", 0,
       "'denoted' must not match the note/letter/news heuristic"),
  list("crossref", "", "J Hepatol",
       "Footnotes to a hepatology dataset", "unclassified", 0,
       "'footnotes' must not match either"),
  list("crossref", "", "J Hepatol",
       "Newsletter of hepatic imaging", "other", 0,
       "'newsletter' still does match, deliberately"),
  list("europepmc", "Journal Article", "Research Square",
       "Liver fibrosis staging by MRE", "unclassified", 1,
       "preprint detected from the journal name, not the source"),

  # --- ClinicalTrials.gov special case ---
  list("clinicaltrials", "Trial: INTERVENTIONAL", "",
       "A Study of Resmetirom in NASH", "clinical_trial", 0, "CT.gov interventional"),
  list("clinicaltrials", "Trial: OBSERVATIONAL", "",
       "Registry of MASLD Patients", "observational", 0, "CT.gov observational")
)

recs <- data.frame(
  source  = vapply(cases, `[[`, "", 1),
  type    = vapply(cases, `[[`, "", 2),
  journal = vapply(cases, `[[`, "", 3),
  title   = vapply(cases, `[[`, "", 4),
  stringsAsFactors = FALSE
)
exp_st <- vapply(cases, `[[`, "", 5)
exp_pp <- as.integer(vapply(cases, `[[`, 0, 6))
why    <- vapply(cases, `[[`, "", 7)

out <- classify_study_type(recs)

fail <- 0L
for (i in seq_len(nrow(recs))) {
  ok_st <- identical(out$study_type[i], exp_st[i])
  ok_pp <- identical(out$is_preprint[i], exp_pp[i])
  if (!ok_st || !ok_pp) {
    fail <- fail + 1L
    cat(sprintf("  [FAIL] %s\n         title=%s\n         expected %s/pp=%d  got %s/pp=%d\n",
                why[i], recs$title[i], exp_st[i], exp_pp[i],
                out$study_type[i], out$is_preprint[i]))
  } else {
    cat(sprintf("  [PASS] %-58s -> %s%s\n", why[i], out$study_type[i],
                if (out$is_preprint[i] == 1) " (preprint)" else ""))
  }
}

# every bucket in the published taxonomy must be reachable from this table
missing <- setdiff(STUDY_TYPE_ORDER, unique(out$study_type))
if (length(missing)) {
  cat("  [FAIL] taxonomy buckets never exercised: ", paste(missing, collapse=", "), "\n")
  fail <- fail + 1L
} else {
  cat("  [PASS] every bucket in STUDY_TYPE_ORDER is exercised\n")
}

cat("\n", nrow(recs), " cases, ", fail, " failure(s)\n", sep = "")
quit(status = if (fail > 0) 1L else 0L)
