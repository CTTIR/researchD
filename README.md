# researchD

Self-hosted literature and signal surveillance for research teams. Define a
question, retrieve overlapping sources on a schedule, inspect ranked results,
and deliver them through RSS, email, local digest files or Zotero.

This is the **server**. [**researchR**](https://github.com/CTTIR/researchR)
is its R client for defining questions and retrieving typed tibbles through
the token-authenticated REST API.

**Development software for academic research.** Search coverage, source access
and classification accuracy vary. Results need human review; this is not a
validated systematic-review search or a clinical decision system. The
[release record](docs/release-2026-10-31.md) documents the audit,
repairs, verification evidence and remaining release work.

## Sources and coverage

| Track | Implemented sources | Important limits |
|---|---|---|
| Literature | PubMed, Europe PMC, OpenAlex, Crossref, Semantic Scholar, ClinicalTrials.gov, bioRxiv/medRxiv | Access limits and date/query semantics differ. PubMed supplies titles and publication types but not abstracts; overlapping sources can fill missing metadata. |
| Signals | NIH RePORTER, native ClinicalTrials.gov registrations and posted results, openFDA, SEC filings, PatentSearch, configured RSS and scrape targets | A registry or agency source is authoritative about provenance, not evidence quality. FDA label dates are not approval dates. |
| Registry leads | CTIS and ISRCTN through optional `ctrdata` | Lead records, without reliable registration/result event dating; live behavior has not been verified in this maintenance pass. |
| Unavailable adapters | EPO, Cortellis, Citeline | Selecting these reports an unavailable source; it does not provide commercial-database coverage. |

OpenAlex uses the native HTTP API. An API key is required for cursor
pagination; configure one in the question's encrypted API keys. See the
[OpenAlex paging documentation](https://help.openalex.org/api/paging/).
PatentSearch requires a PatentsView key; its adapter uses the current
`X-Api-Key` header. Semantic Scholar may rate-limit keyless access. Sources
requiring registration or subscription are not made accessible by installing
researchD. WHO ICTRP and comprehensive worldwide trial-registry coverage are
outside the present scope.

`config/settings.yaml` sets `fetch.max_records` (default 5000; values above
100000 are capped). Fetches have additional upstream limits: PubMed at 10000,
Semantic Scholar at 1000 per synonym query, and openFDA at 25000. bioRxiv/medRxiv caps apply to
records scanned before keyword filtering. NIH, SEC and PatentSearch use
bounded first pages. A reported cap or incomplete response is recorded in
source health, retains any usable rows, and makes the pipeline run exit
nonzero. It must not be interpreted as a complete search.

Classification uses conservative rules and available metadata; it has not
been validated against an independently labelled scientific corpus. Ranking
orders retrieved records and does not discard low scores. Deduplication
prefers richer metadata and stores contributing sources and filled fields in
`provenance`. Existing classified rows are not automatically relabelled on
upgrade. Improved Unicode/missing-year title keys can cause some older
records without DOIs to appear once again.

## Three services, one database

| Service | Runtime | Role |
|---|---|---|
| `pipeline` | R and cron | retrieve, deduplicate, classify, rank, persist; file and Zotero delivery |
| `web` | Python/FastAPI | public feeds; password + TOTP dashboard; token API and ingest |
| `scheduler` | Python | per-question email cadence and instant signal alerts |

The services share `db/pipeline.sqlite` using WAL and a busy timeout. The
Compose stack binds `db/`, `digests/` and `config/` to the host. Keep the
SQLite database on local storage shared by these processes, on one host.
Concurrent pipeline runs and scheduler ticks are separately locked against the
same database to avoid overlapping delivery. Multi-host
deployment requires a different database and coordination design.

## First run on this machine

Prerequisites: Docker Engine/Desktop with Compose v2, Git, and Python 3 for
one-time secret generation. The pipeline image uses R 4.4.1; the web and
scheduler images use Python 3.12. Native server development on Linux/macOS
supports R 4.4+ and Python 3.12+; Windows hosts should use Linux containers.
ARM builds compile R packages from source. The image includes their system
libraries and uses two package-install workers; pass
`docker compose build --build-arg R_INSTALL_CPUS=1` to reduce build concurrency.

```bash
git clone https://github.com/CTTIR/researchD.git
cd researchD
python3 scripts/setup.py
docker compose up -d --build --wait
curl --fail http://127.0.0.1:8000/healthz
```

Back up **`APP_SECRET_KEY` separately from the database**. Changing or losing
it makes stored encrypted source keys unreadable. The engine reports the affected
question as failed; restore the matching key or replace its stored credentials.
Keep `.env` private and
out of version control.

The setup command creates a private `.env`, refuses to overwrite an existing
one, makes `db/` and `digests/` private (0700), and leaves external delivery disabled. Use `--port 18084 --project researchd-test`
for a separate stack, or `--base-url https://feeds.example.org` for an HTTPS
origin behind your reverse proxy.

1. Edit `.env` and `config/settings.yaml` with a real contact email before
   retrieving sources. Configure SMTP and Zotero when you intend to use them.
2. Open <http://127.0.0.1:8000/setup>, create the administrator, log in, and
   complete **2FA enrollment**. Scan the QR code and save the recovery codes.
   Each successful authenticator code can be used once. If you sign in again
   immediately after enrollment, wait for a new code. Recovery codes are also single use. Keep the enrolled
   authenticator: self-service replacement is not yet available.
3. Create a question in the dashboard, reviewing source toggles, lookback,
   delivery choices and required source keys. New installations have no active
   questions. For YAML seeding, review `config/questions.example.yaml` and copy
   it to `config/questions.yaml` before the first engine run; seeding occurs
   only while the question table is empty. The example uses file delivery.
4. Fetch explicitly and inspect the outcome:

   ```bash
   docker compose exec pipeline Rscript R/run_all.R
   docker compose logs --tail=100 pipeline web scheduler
   ```

   Select one question with `docker compose exec pipeline Rscript R/run_all.R YOUR_QUESTION_ID`.
   The first run searches the entire lookback window and may be slow. Inspect
   source health as well as records; zero results alone do not prove that every
   source worked. With no enabled questions, the engine records a healthy idle run.

The local HTTP settings above work only on this machine. For remote access,
configure TLS as described below before exposing the service. Do not enable
`RUN_ON_START` until your questions and delivery destinations are reviewed.

To use R, install the client and use the API token from `.env`:

```r
install.packages("remotes")
remotes::install_github("CTTIR/researchR")
library(researchR)
# Set RESEARCHR_TOKEN in your private R environment to the .env API_TOKEN.
con <- rr_connect("http://127.0.0.1:8000", token = Sys.getenv("RESEARCHR_TOKEN"))
rr_questions(con)
rr_records(con, "YOUR_QUESTION_ID")
rr_source_health(con)
```

`rr_connect()` checks reachability by default. `check = FALSE` only constructs
a client and does not verify a working connection.

## Endpoints and access

| Surface | Authentication | Main routes |
|---|---|---|
| Public | None | `/healthz`, `/feed/{id}.xml`, `/feed/{id}/all.xml`, `/feed/{id}/{study_type}.xml`, `/feed/{id}/preprints.xml`, `/signals/{id}.xml`, `/signals/{id}/all.xml` |
| Dashboard | Password, TOTP and session cookie | `/setup`, `/login`, `/2fa`, `/2fa/enroll`, `/`; `/api/questions`, `/api/signal_config/{id}`, `/api/source_health`, `/api/heartbeat` |
| Client API | Bearer `API_TOKEN` | `/api/v1/meta`, `/api/v1/questions`, `/api/v1/records`, `/api/v1/signals`, `/api/v1/source_health`, `/api/v1/heartbeat`, `/api/v1/signal_config/{id}` |
| External ingest | Bearer `INGEST_TOKEN` | `POST /api/ingest` |

These credentials are separate: a dashboard session cannot authenticate the
client API, and the client token cannot authenticate ingest. An unset token
disables its surface with HTTP 503. Feed URLs and feed contents are public;
do not put confidential research into a deployment with public feed access.

The curated feed and email honor the question's threshold. An `all.xml` feed
removes that threshold but is still limited to the newest `FEED_LIMIT` rows
(default 100). Retrieve the stored collection through the paginated API.
Offsets are not a transactional snapshot: avoid concurrent ingestion when
exporting an exact dataset. Below-threshold records remain stored until
explicit retention pruning and may become eligible when thresholds change.

## Schedules and delivery

`CRON_SCHEDULE` defaults to daily 06:00 for the full pipeline;
`CRON_FAST_SCHEDULE` defaults to hourly at :15 for signals only. Set `TZ`
to the intended time zone. A manual run can select `RUN_MODE=lit` or
`RUN_MODE=signals`; the default is `all`. Failed questions or incomplete
sources do not stop the other questions from being processed, but cause a
nonzero exit and a degraded pipeline heartbeat.

Email has its own per-question `email_cron`, interpreted in `TZ`, and
recipient list. The scheduler checks every `CHECK_INTERVAL` seconds. Instant
alerts send eligible high-priority signals on a subsequent tick. Records are
marked emailed only after successful delivery. A partial SMTP recipient
failure, or a crash after SMTP acceptance but before the SQLite acknowledgment,
can cause already-accepted recipients to receive a duplicate on
retry; there is no per-recipient delivery ledger. Unknown signal types remain
visible in digests instead of being silently dropped.

File and Zotero delivery use persisted pending state for new records,
including records received through ingest. Failed delivery is retried by a
later full/literature run even if no new source records arrive. Digest files
have unique names; Zotero retains remote item keys for retry reconciliation.
A process failure after writing files but before acknowledging them in SQLite
can leave duplicate digest files on retry; their contents are preserved.
Existing historical rows are not replayed automatically after this upgrade.
Signals reach users through feeds and the email scheduler. The old global
`email` block in settings belongs to the legacy direct-delivery helper;
normal service email is configured through `.env` and each question.

## TLS reverse proxy

The web service speaks HTTP on port 8000. For production, set `BASE_URL` to
your public HTTPS URL and `SECURE_COOKIE=1`. Compose binds the host port to
`127.0.0.1` by default through `WEB_BIND`; it is not an Internet listener.

For Caddy running **on the host**, configure your domain's DNS, allow inbound
ports 80/443 for Caddy, and use:

```caddyfile
feeds.example.org {
    reverse_proxy 127.0.0.1:8000
}
```

For Caddy **in a container**, join it to the same Compose network and use
`reverse_proxy web:8000`. Container-local `127.0.0.1` points to Caddy itself,
so the host example cannot be copied unchanged into a proxy container. Keep
port 8000 private and protect the proxy's certificate/configuration state.

The encryption key protects a stolen database or backup, not a compromised
host holding both `.env` and the database. Authentication attempt limits are
in-process and reset when the web service restarts; use an additional proxy
rate limit if you need a persistent boundary or multiple web workers.

## Operations

Back up SQLite through its backup API, not by copying an actively written
main database without its WAL:

```bash
scripts/backup.sh db/pipeline.sqlite /var/backups/researchD 14
```

The Python 3 helper checks integrity, gives each snapshot a unique filename,
and keeps the newest 14 snapshots. Use the path printed by the backup command. Schedule it
on the host and back up `APP_SECRET_KEY` separately. To restore, first stop
all writers; the restore script validates the backup and preserves the replaced
database together with any WAL/SHM recovery files:

```bash
docker compose down
scripts/restore.sh /var/backups/researchD/pipeline-YYYYMMDD-HHMMSS-SUFFIX.sqlite db/pipeline.sqlite
docker compose up -d
```

Retention is opt-in; otherwise records, signals, fetch logs and digest files
grow without bound:

```bash
scripts/prune.sh db/pipeline.sqlite digests
```

The default removes records/signals older than 24 months, limits fetch logs
to 5000 rows and keeps 60 files of each digest format. Records with unfinished
file or Zotero delivery remain stored until delivery succeeds; disable or
resolve a stale destination before expecting those records to expire. Read the script's arguments before
changing these limits. Pruning frees deduplication keys, so a later fetch
can rediscover a removed record. Verify backup/restore on a disposable copy
before relying on a deployment.

On Linux, files created in bind mounts can be owned by container root.
Run host maintenance commands with the required filesystem permissions
(for example, `sudo scripts/prune.sh db/pipeline.sqlite digests`); do not make
state directories world-writable. The database includes authentication state
as well as research records. Keep `db/`, digests and backups private.

Monitor source health (`last_ok`, `last_nonempty`, errors and staleness) and
service heartbeats in the dashboard or client API. `/healthz` is web liveness,
not evidence that the pipeline, scheduler or upstream sources are healthy.
The engine checkpoints the WAL after each run. Configure SMTP delivery and
sender-domain authentication with your chosen relay before enabling email.

## Native development and tests

Python dependencies are pinned in `requirements-web.txt`,
`requirements-scheduler.txt`, `requirements-test.txt` and
`constraints-python.txt`. R adapters are installed during Docker builds.
Runtime startup reports missing packages and never installs them itself.

For native development, create the Python environment and install the core
R and client test dependencies (Node.js 24 is needed only for the dashboard
DOM regression):

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-test.txt
Rscript -e 'install.packages(c("yaml", "DBI", "RSQLite", "filelock", "httr2", "jsonlite", "htmltools", "cli", "tibble", "testthat", "pkgload"), repos = "https://cloud.r-project.org")'
PATH="$PWD/.venv/bin:$PATH" dev/tests/run_all.sh
```

To run the actual adapters natively, install their complete declared set:

```bash
Rscript -e 'source("R/00_utils.R"); install.packages(REQUIRED_PKGS, repos = "https://cloud.r-project.org")'
PATH="$PWD/.venv/bin:$PATH" Rscript R/run_all.R
```

Native processes do not load `.env` automatically. Supply the same exported
encryption key and required service variables when using encrypted question
keys; configure `config/settings.yaml` and `config/questions.yaml` before
running the engine. `.venv/` and the test scratch library/database under
`dev/state/tmp/` are ignored by Git.

The offline suite uses disposable SQLite databases, synthetic upstream and
SMTP responses, and regression checks across the engine, web and scheduler.
The client integration starts a real local HTTP server with a synthetic
database and requires a sibling client checkout:

```bash
git clone https://github.com/CTTIR/researchR.git ../researchR
PATH="$PWD/.venv/bin:$PATH" dev/tests/integration_client.sh
```

Alternatively set `RESEARCHR_PKG` to a client checkout. This integration
does not contact biomedical databases or send real email. The opt-in
`dev/tests/run_all.sh --live` also calls upstream APIs through
`dev/tests/live_fetchers.R`; skipped sources are evidence gaps, not verified
successes. It may incur provider quota usage and requires the full adapter
dependencies and any source credentials.

GitHub Actions defines the offline/native client integration and a separate
Compose build/start smoke test on x86-64 and ARM64, including file export, RSS and an isolated SMTP
socket that captures a synthetic message without forwarding it. The workflow can be dispatched with a
`client_ref` to test a matching researchR branch or commit; ordinary runs use
its main branch. Configuring this workflow does not establish that a hosted
run or a Docker build has succeeded; consult the maintenance record and
actual workflow results.

## License

MIT License. Copyright (c) 2026 Raban Heller. See [LICENSE](LICENSE).

## Use of LLM tools

Where applicable, LLM tools were used for narrowly defined, non-authorial tasks:
copyediting, prose refinement, Markdown/LaTeX formatting, boilerplate scaffolding
(such as CI configurations and build scripts), and code refactoring. Tools included
[Chat AI](https://kisski.gwdg.de/leistungen/2-02-llm-service/), the KISSKI
(GWDG) service, and self-hosted **Mistral Small (24B, Apache-2.0)** served through
a local **vLLM architecture** on an Ubuntu Linux workstation with Intel
processors and NVIDIA GPUs. The self-hosted model ran locally, with no data sent
to third parties.

All scientific claims, methodological choices, analyses, interpretations and
conclusions remain the authors' own. Where applicable, LLM-generated material
must be reviewed and revised before inclusion, and references must be verified
against their DOI, arXiv ID or ISBN.

Local software setup: vLLM with Python/PyTorch, plus R and RStudio.
