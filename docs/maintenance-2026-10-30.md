# Connected maintenance release

Repository naming update, 2026-10-30: `CTTIR/researchd` and its local checkout
were renamed to `CTTIR/researchD` / `researchD`. The audit below preserves the
original name in historical evidence. Current installation links and CI paths
use `researchD`; service endpoints, database formats and credentials are unchanged.

Baselines: researchd `ac71e59a1fa6484c2a82d0b32165af4956b6eaf3` and researchR
`3e1abfeca85dd5d7390a0f19916df9ba73ea3a57`. Both CTTIR checkouts were clean and
matched GitHub main on 2026-10-30. Work uses `dev/connected-hardening-20261030`.
The older private researchR checkout is unchanged. No source database or real
recipient account was modified. Publication and deployment are separate steps.

## Purpose and release criteria

Researchers should be able to define questions, collect literature and signals
repeatedly, inspect RSS or typed R tables, and deliver new material without
silently concealing retrieval or delivery errors. This is research surveillance
software, not a validated systematic-review search or study-design adjudicator.

Local acceptance requires enforced second-factor enrollment, usable question
editing, exported cron credentials, explicit failed/incomplete retrieval,
additive database upgrades, preserved source metadata and signal revisions,
retryable delivery, safe recovery, compatible R API results and a reproducible
first-use path. Container startup, visual browser enrollment and configured
external delivery remain deployment acceptance checks rather than inferred
successes from unit tests.

## Roadmap presented before implementation

| Milestone | Outcome and concrete changes | Dependencies | Completion evidence |
|---|---|---|---|
| Essential repairs | MFA enrollment guard; exported cron credentials; dashboard editing; source-health propagation; date normalization and conservative classification | Existing session and API contracts | Targeted failure regressions and existing offline suite |
| Dependable daily use | Signal revisions, dedup provenance, durable file/Zotero queue, SMTP refusal handling, stable client types and timestamps, safe retention/recovery | Additive schema and coordinated client update | Migration, retry, real entrypoint, backup/restore and HTTP client journeys |
| Maintainable distribution | Pinned Python manifests, server/connected CI, documented source limits and exact first-use commands | Repaired workflows | Package build/check, advisory scan, workflow lint and native startup; container check remains external |
| Deferred | Snapshot pagination, per-recipient SMTP ledger, complete EPO/commercial adapters, independently validated scientific classification and high-volume benchmarking | Provider access and measured product requirements | Separate scoped work; no current-support claim |

The first two milestones implement local acceptance. The third supplies a
reviewable release candidate and automated container validation; it does not
establish production operation. No broad rewrite or new source product was
added. Native OpenAlex HTTP requests replaced the adapter dependency because
pagination, authentication and failure reporting needed explicit control.

## Baseline evidence

The original R package built and passed `R CMD check --no-manual`; original
real HTTP client integration passed. The original server passed 15/16 test
groups. Its restart fixture used `now - 1 hour` with a 07:00 schedule; between
07:00 and 08:00 a real due time intervenes. That failure was a test-clock bug,
not demonstrated production resending. A fixed clock now tests the intended
restart behavior. Passing baseline tests did not cover the defects below.

## Confirmed defects and implemented repairs

Severity refers to user impact in this single-host research application.
Locations identify the affected implementation; the accompanying `hardening_*`
checks and existing suites supply executable regression evidence.

| Severity | Evidence and location | User impact | Repair |
|---|---|---|---|
| High | A password-only session could request an enrolled user's current TOTP secret through `web/app.py` enrollment routes. | The second factor could be bypassed. | Both enrollment routes reject enrolled accounts before exposing a secret; enrollment/recovery consumption is serialized and authentication attempts are bounded. |
| High | `entrypoint.sh` wrote unexported, unquoted environment assignments. | Scheduled retrieval lost credentials; shell syntax in values changed their meaning. | A 0600 allowlisted, shell-quoted export file reaches R and its decryption helper. Synthetic quote/newline tests verify exact round trips without execution. |
| High | `web/decrypt_key.py` converted unreadable encrypted credentials to empty strings, while `load_questions_db()` omitted failed loads from status. | Wrong/missing master keys silently downgraded retrieval and broken questions could disappear behind a healthy heartbeat. | Fixed secret-free diagnostics, failed-load tracking, selected-question isolation and nonzero/degraded status; unrelated valid questions continue. |
| High | `web/templates/dashboard.html` double-parsed question JSON and interpolated stored values into HTML/handlers. | Editing failed; hostile stored content could become executable UI. | Use decoded objects and DOM text/value/event APIs; retain signal settings when their load fails. DOM regressions exercise both paths. |
| High | Nested requests in `R/02_fetch.R` and signal orchestration swallowed failures. | An outage or truncated retrieval could look like a complete empty search. | Preserve partial rows with explicit source errors, bounded paging and degraded pipeline heartbeat/exit status. Unsupported EPO/Cortellis/Citeline adapters fail explicitly. |
| High | `R/08_signals.R` supplied compact FDA dates and invented dates for unknown events; registry events were not reliably distinguished. | Ranking crashed or old/undated leads appeared fresh. | Normalize dates, keep unknown dates missing, use native ClinicalTrials.gov registration/result-posting events and label optional registry results as leads. FDA label dates are not approval dates. |
| High | `R/09_signals_persist.R` and `/api/ingest` ignored changed rows with an existing identifier. | Trial results and other material updates disappeared. | Transactional append-only revisions share comparison/date/key semantics across R and Python, including exact changed numeric values and JSON booleans. |
| High | `R/05_deliver.R` assumed HTTP 200 meant Zotero acceptance and had no retry state. Same-day digest files overwrote prior output. | Items could be lost, duplicated after uncertain responses, or overwritten locally. | Atomically enqueue new records with persistence; check per-item API outcomes, save remote keys/destination before writes, reconcile retries, drain pending items independently of new retrieval and use unique digest files. Ingest uses the same outbox. |
| High | `web/scheduler.py` treated partial SMTP refusal as success and acknowledged categories omitted from rendering. | Some recipients or signal types silently lost delivery. | Failed sends remain pending, all stored categories are visible, metadata and links are escaped, malformed questions are isolated and heartbeat reports failures. Partial refusal can still duplicate mail to accepted recipients on retry. |
| High | `scripts/{backup,restore,prune}.sh` split filenames through shell pipelines; restore discarded old WAL/SHM; pruning could orphan delivery state. | Unsafe retention, loss of committed recovery data, suppressed later delivery. | Use path APIs and validated positive counts, unique integrity-checked snapshots, staged restore with old DB/WAL/SHM preserved, foreign-key cascades and retention of unfinished delivery payloads. |
| Medium | `R/06_classify.R` treated controlled trials as randomized and generic journal articles as observational. | Unsupported study-design labels misled filtering. | Require evidence for randomized/observational labels; retain unclassified records. Reclassification also refreshes canonical ranking and preprint flags for ingested records that remain unclassified. |
| Medium | `R/03_dedup.R` discarded richer duplicate fields and conflated some missing/Unicode identities. | Abstracts and source contributions were lost. | Fill missing metadata in source-priority order, retain structured provenance and preserve Unicode distinctions. Existing classified rows are not silently relabelled. |
| Medium | Empty-source `rbind` orchestration created a one-cell logical matrix. | A deliberately source-disabled or ingest-only pipeline failed during startup. | Typed zero-row binders for both tracks; real command-line ingestion/export tests cover repeated full/literature runs. |
| Medium | Pipeline invocations shared external delivery state without a process lock; first-run midnight schedules were never due. | Concurrent runs could duplicate output; midnight questions could remain unsent. | Database-adjacent process lock with contention/release test; named-time-zone scheduling and fixed-clock midnight/DST cases. |
| Medium | API pagination had non-unique score/date ordering; inputs were loosely validated. | Equal-ranked results and malformed definitions could produce unstable reads or operational errors. | Add a unique ordering tie-breaker; validate new IDs, booleans, limits, schedules and source definitions. Offset pages remain non-snapshot reads. |
| Medium | researchR timestamp parsing discarded time/offset, empty tables changed types, HTTP error braces crashed formatting. | Valid analysis and error handling broke even though baseline package checks passed. | UTC/fraction/offset parsing, consistent typed schemas, structured provenance, safe error text, validated paging and encoded legacy read paths. See the client maintenance record. |
| Medium | Runtime package installation and unpinned Python dependencies changed deployments implicitly; the server lacked CI. | Startup depended on registries; builds and interface drift were hard to reproduce. | Explicit preparation, pinned Python constraints, declared core/optional R dependencies, connected native CI and Compose smoke workflow. Local Docker contexts exclude secrets/runtime data. |

The remote enrollment QR script was also replaced with a bundled MIT-licensed
qrcodejs asset, including license, source URL and checksum. The existing project
license and author metadata are preserved. A recognizable-credential pattern
scan found no matched credentials in the reviewed working files; this is not a
proof that every possible secret form is absent.

## Verification

Executed on macOS ARM64 with R 4.6.1, Python 3.15.0 and Node.js 26.12.0.
Tests ran in isolated copies/databases; no real recipient or production state
was used. The supported Docker versions (R 4.4.1/Python 3.12) are CI targets,
not locally executed environments. Installed Python package metadata permits
Python 3.12; that check alone is not a container test.

| Check | Observed result |
|---|---|
| Final `dev/tests/run_all.sh` | **25 passed, 0 failed**. Includes R parsing/classification/dedup/fetch tests, schema/migration and concurrent SQLite tests, API/auth/feed checks, scheduler, DOM, cron, real CLI and operations. |
| Real CLI failure paths | Passed: repeated queued ingest export, process lock/release, invalid mode, mixed/all-invalid/selected questions, missing/wrong master key, repaired credentials and signal failure isolation. |
| Retention/recovery regressions | 11 tests passed: unfinished deliveries, cascades, unusual paths, invalid counts, unique backups, committed/uncommitted WAL boundaries and corrupt/same-file restore safeguards. |
| Updated researchR against updated researchd | Real localhost HTTP suite passed, including authentication, encrypted key round trip, question CRUD, paging, typed results, signal configuration and health. Hardened harness also rejects an occupied port promptly and cleans up. |
| Updated researchR against original researchd | Real HTTP backward-compatibility suite passed; optional provenance remains usable when absent. |
| researchR 0.1.1 package | Build and fresh installation passed; `R CMD check --no-manual` reports **Status: OK** with no errors, warnings or notes. Offline installed tests: 106 passed, 12 expected server-dependent skips. Those integrations were separately exercised over HTTP. |
| Dependency validation | `pip check` passed. `pip-audit` on pinned web/scheduler runtime dependencies reported no known vulnerabilities on 2026-10-30. This is a current advisory check, not assurance about unknown vulnerabilities or the complete unbuilt image/R stack. |
| Source smoke probes | OpenAlex, ClinicalTrials.gov and Crossref each returned one public result. Deliberate one-record caps produced explicit incomplete-source status, as intended. NIH RePORTER public response was also inspected. These are endpoint smoke checks, not exhaustive coverage validation. |
| Static distribution checks | Python compile, shell syntax, workflow actionlint and Git whitespace checks passed. CI fixture/secret-generation snippets were exercised locally. |
| Not performed | Docker build/Compose runtime (no installed runtime), actual browser enrollment/rendering (browser-control host unavailable), newly hosted CI, real SMTP/Zotero writes, CRAN submission and production deployment. |

Evidence logs, the final client tarball and checksums are preserved locally in
`/Users/rh/Documents/GitHub/.repository-audit/2026-10-30-research-pair/`.
The repository records remain the portable audit; that machine-local directory
contains the detailed run receipts. Earlier intermediate failures remain
separate from the final passing result. No check was weakened to obtain it.

## Compatibility and operating limits

- Upgrade both services from the same server checkout. R and Python apply the
  same additive `records.provenance` migration and create the delivery outbox.
  Existing records are retained and historical rows are not replayed through
  the new outbox automatically. Back up the database and master key first.
- The client remains API v1-compatible, including servers without provenance.
  Question writes remain full replacements. New writes use documented slug
  IDs; representable legacy IDs can still be read through encoded paths.
- Improved Unicode/missing-year dedup keys can cause an older DOI-less row to
  reappear once. Dedup and classification are heuristics, not identity or
  scientific-quality adjudication. Previously classified data is not silently
  changed. Reclassification of legacy collections requires separate review.
- Retrieval is bounded. OpenAlex cursor continuation needs a key; NIH, SEC and
  PatentSearch retain bounded first-page behavior. CTIS/ISRCTN live integration,
  provider-specific quotas and every optional adapter were not verified here.
  Commercial/EPO coverage is explicitly unavailable. Source health reports
  detected truncation; absence of an error does not prove exhaustive coverage.
- Public RSS and signal feeds expose their contents without authentication;
  they are bounded recent views. Use the authenticated API for retained rows,
  and avoid concurrent ingestion when an exact offset-paginated export matters.
- File delivery is at least once across a crash between writing and SQLite
  acknowledgment. SMTP lacks a per-recipient ledger. Changing a pending Zotero
  destination requires review; an uncertain existing remote item must match
  the queued identity before acknowledgment. Real external sends were not made.
- Keep SQLite on local single-host storage. Stop all writers before restore.
  Back up the encryption key separately; preserve recovery bundles together.
  Pending delivery records intentionally survive ordinary retention.
- Authentication throttles are process-local. Multi-worker or public-facing
  deployments need a suitable TLS proxy and persistent request limits. Resource
  usage under large workloads, accessibility and visual browser behavior need
  deployment-specific validation. No high-volume performance claim is made.

## First use and remaining release actions

The [README](../README.md#first-run-on-this-machine) gives exact Docker/Compose
prerequisites, safe secret creation, first question, manual fetch, feeds/client,
TLS and recovery commands. The native test path installs Python/R dependencies
explicitly and starts a disposable real HTTP server; it sends no real email.

Before production or a published release:

1. On a machine with Docker/Compose, build all three images and execute the
   provided synthetic Compose smoke check. Confirm scheduled invocation as
   well as the manual native entrypoint. Hosted CI is not yet observed.
2. Complete setup, enrollment, question editing and feed browsing in a real
   browser. The browser-control host was unavailable during this audit;
   passing executable DOM tests does not replace this check.
3. Configure the intended provider credentials and a controlled delivery
   destination, exercise a complete scheduled cycle, and verify actual receipt
   and recovery from a disposable backup. No real SMTP or Zotero writes were
   authorized/performed in these checks.
4. Review both local branches together, then publish or deploy only under the
   owner's authorization. No production deployment, hosted acceptance, CRAN
   submission or remote release is claimed.

Primary contracts consulted: [Zotero write responses](https://www.zotero.org/support/dev/web_api/v3/write_requests),
[OpenAlex paging](https://help.openalex.org/api/paging/),
[OpenAlex authentication](https://help.openalex.org/api/authentication/),
[PatentSearch API](https://github.com/PatentsView/PatentSearch-API/blob/main/docs/docs/Search%20API/SearchAPIReference.md),
[openFDA label schema](https://open.fda.gov/fields/druglabel.yaml), and
[MeSH Controlled Clinical Trial](https://www.ncbi.nlm.nih.gov/mesh/68018848).
