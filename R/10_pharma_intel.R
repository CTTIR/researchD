# ============================================================
# 10_pharma_intel.R — adapter for COMMERCIAL pharma-intelligence
# APIs (Cortellis / Citeline-Pharmaprojects / Trialtrove / etc.).
#
# STATUS: SCAFFOLD. These APIs are enterprise-gated — no public
# docs, credentials issued per-contract, response shapes vary by
# product and contract. This file deliberately does NOT invent the
# wire format. It provides:
#   - a normalized interface that emits the SAME signals schema as
#     every other source (so nothing downstream changes when live)
#   - the auth / pagination / rate-limit scaffolding these APIs
#     universally require
#   - explicit TODO markers at each point that must be completed
#     from the real API docs you receive WITH access
#
# When you get access, you fill in three things per provider:
#   1. the auth handshake (.pi_auth_*)
#   2. the request URL + query mapping (.pi_request_*)
#   3. the response → normalized-record mapping (.pi_parse_*)
# Everything else (orchestration, dedup, ranking, dedup keys,
# confidence) already works.
#
# Credentials come in via settings$signal_keys$<provider> (decrypted
# per-question, same path as every other key) — NEVER hardcoded.
# ============================================================

# Normalized output = the signals schema (see 08_signals.R .empty_signals()).
# These providers map naturally onto our signal_type vocabulary:
#   drug pipeline entry / dev-status change -> "regulatory" or "trial_result"
#   trial record                            -> "trial_registration"/"trial_result"
#   deal / company event                    -> "press_release" (curated, so
#                                               confidence "reported" not "unverified")
# Confidence for these is "reported" by default: human-curated and reliable,
# but still a secondary source summarizing a primary event.

# --- provider registry ----------------------------------------
# Each provider declares the pieces the generic runner needs. The
# fn slots point at provider-specific implementations below.
.PHARMA_PROVIDERS <- list(
  cortellis = list(
    label   = "Cortellis (Clarivate)",
    # TODO[access]: confirm base URL from your Cortellis API docs.
    base    = "https://api.cortellis.com",          # PLACEHOLDER
    auth_fn = ".pi_auth_oauth2",                     # Clarivate uses OAuth2
    req_fn  = ".pi_request_cortellis",
    parse_fn= ".pi_parse_cortellis"
  ),
  citeline = list(
    label   = "Citeline / Pharmaprojects + Trialtrove (Norstella)",
    # TODO[access]: confirm base URL from your Citeline API docs.
    base    = "https://api.citeline.com",            # PLACEHOLDER
    auth_fn = ".pi_auth_apikey",                     # often header API key
    req_fn  = ".pi_request_citeline",
    parse_fn= ".pi_parse_citeline"
  )
)

# ============================================================
# 1. AUTH — fill the handshake when you have credentials/docs
# ============================================================

# OAuth2 client-credentials (typical for Clarivate-class APIs).
# Returns a bearer token string, or "" on failure.
.pi_auth_oauth2 <- function(provider, creds) {
  # creds is a list: expects $client_id, $client_secret (from decrypted key,
  # which you'd store as JSON "id:secret" or a small JSON blob).
  if (is.null(creds$client_id) || is.null(creds$client_secret)) return("")
  # TODO[access]: replace token_url + body with the real values from docs.
  token_url <- paste0(provider$base, "/oauth2/token")   # PLACEHOLDER
  resp <- tryCatch(
    httr2::request(token_url) |>
      httr2::req_body_form(grant_type = "client_credentials",
                           client_id = creds$client_id,
                           client_secret = creds$client_secret) |>
      httr2::req_perform(),
    error = function(e) NULL)
  if (is.null(resp)) return("")
  tok <- tryCatch(httr2::resp_body_json(resp)$access_token, error = function(e) "")
  tok %||% ""
}

# Simple header API key (typical for some Citeline/Norstella endpoints).
.pi_auth_apikey <- function(provider, creds) {
  # creds$api_key is the raw key. Nothing to exchange; return it.
  creds$api_key %||% ""
}

# ============================================================
# 2. REQUEST — map a question to a provider query
# ============================================================
# Each returns a list(url=, headers=, query=) ready for httr2, or NULL
# to skip. Use the question's concept terms + lookback window.

.pi_request_cortellis <- function(provider, q, token, days_back) {
  terms <- .signal_terms(q)
  if (length(terms) == 0) return(NULL)
  from <- format(Sys.Date() - days_back, "%Y-%m-%d")
  # TODO[access]: replace path + param names with real Cortellis search API.
  # Cortellis has separate endpoints for drugs / trials / news — you'll likely
  # call more than one and merge. This stub targets a generic search path.
  list(
    url = paste0(provider$base, "/search/v2/drugs"),            # PLACEHOLDER
    headers = list(Authorization = paste("Bearer", token)),
    query = list(q = paste(terms, collapse = " OR "),           # PLACEHOLDER
                 updatedSince = from, limit = 100)
  )
}

.pi_request_citeline <- function(provider, q, token, days_back) {
  terms <- .signal_terms(q)
  if (length(terms) == 0) return(NULL)
  from <- format(Sys.Date() - days_back, "%Y-%m-%d")
  # TODO[access]: replace with real Pharmaprojects/Trialtrove query API.
  list(
    url = paste0(provider$base, "/v1/search"),                  # PLACEHOLDER
    headers = list(`x-api-key` = token),
    query = list(query = paste(terms, collapse = " OR "),       # PLACEHOLDER
                 since = from, pageSize = 100)
  )
}

# ============================================================
# 3. PARSE — map provider JSON to normalized signals rows
# ============================================================
# Each takes the parsed JSON body and returns a data.frame in the
# signals schema. THIS is where you encode the real response shape
# once you can see a live payload.

.pi_parse_cortellis <- function(js, provider) {
  # TODO[access]: replace with the real result path + field names.
  results <- js$results %||% js$data %||% list()             # PLACEHOLDER
  if (length(results) == 0) return(.empty_signals())
  do.call(rbind, lapply(results, function(r) {
    # PLACEHOLDER field mapping — adjust to actual payload:
    data.frame(
      signal_type = .pi_infer_type(r),
      source = "cortellis",
      ext_id = r$id %||% r$entityId %||% "",
      title  = r$name %||% r$title %||% "",
      summary= substr(r$summary %||% r$description %||% "", 1, 1500),
      org    = r$company %||% r$sponsor %||% "",
      signal_date = r$updatedDate %||% r$date %||% format(Sys.Date(), "%Y-%m-%d"),
      url    = r$url %||% "",
      confidence = "reported",
      extra  = jsonlite::toJSON(list(provider = "cortellis",
                phase = r$phase %||% NA, status = r$status %||% NA),
                auto_unbox = TRUE),
      score = 0, stringsAsFactors = FALSE
    )
  }))
}

.pi_parse_citeline <- function(js, provider) {
  # TODO[access]: replace with the real result path + field names.
  results <- js$hits %||% js$results %||% list()             # PLACEHOLDER
  if (length(results) == 0) return(.empty_signals())
  do.call(rbind, lapply(results, function(r) {
    data.frame(
      signal_type = .pi_infer_type(r),
      source = "citeline",
      ext_id = r$id %||% "",
      title  = r$drugName %||% r$trialTitle %||% r$title %||% "",
      summary= substr(r$summary %||% "", 1, 1500),
      org    = r$company %||% r$sponsor %||% "",
      signal_date = r$lastModified %||% format(Sys.Date(), "%Y-%m-%d"),
      url    = r$url %||% "",
      confidence = "reported",
      extra  = jsonlite::toJSON(list(provider = "citeline",
                phase = r$phase %||% NA), auto_unbox = TRUE),
      score = 0, stringsAsFactors = FALSE
    )
  }))
}

# Best-effort map of a provider record to our signal_type vocabulary.
# TODO[access]: refine using the provider's own record-type field.
.pi_infer_type <- function(r) {
  blob <- tolower(paste(r$recordType %||% "", r$type %||% "",
                        r$status %||% "", r$phase %||% ""))
  if (grepl("trial", blob) && grepl("result|complete|readout", blob)) return("trial_result")
  if (grepl("trial", blob)) return("trial_registration")
  if (grepl("approv|regulat|filing|designation", blob)) return("regulatory")
  if (grepl("deal|licens|accquisit|partnership|news", blob)) return("press_release")
  "regulatory"   # safe default for a curated drug-pipeline event
}

# ============================================================
# Generic runner — used by fetch_signals when a provider is enabled
# ============================================================
fetch_pharma_intel <- function(provider_key, q, days_back, settings) {
  # Contract-specific endpoints above are design notes, not verified clients.
  # Never send stored credentials to placeholder domains or report success.
  stop("Commercial pharma adapters are not implemented; verified contract-specific integration is required")
}
