# researchD 0.1.0 — 2026-10-31

First versioned server release, paired with researchR 0.1.2. The repository
and local checkout are now named researchD; service names and existing SQLite
data remain compatible.

- Add one-command private first-use configuration, empty initial question
  settings, separate file-only examples and Compose readiness checks.
- Reject cross-origin browser writes, consume successful authenticator codes
  once, and keep source credentials out of validation and fetch diagnostics.
- Serialize scheduler senders and API key updates; report invalid delivery
  configuration rather than a healthy empty run.
- Preserve YAML source/enabled controls, import seeds transactionally, reject
  corrupt signal settings and report early-ended source pagination.
- Bound future-date recency scores and reject malformed upstream dates.
- Keep MeSH-only concepts in source queries by using heading text where a
  source has no MeSH field, preventing empty queries and unrelated matches.
- Emit valid, escaped RSS for control characters and untrusted text; save a
  consistent dashboard question snapshot across asynchronous requests.
- Extend real HTTP, migration, engine and Compose delivery checks. The Compose
  smoke uses synthetic data and an isolated SMTP capture, with no external sends.
- Install the `libjq` headers needed by ARM source builds and exercise Compose
  on both x86-64 and ARM64 runners, with bounded R package build concurrency.

Back up the database and matching encryption key before upgrading. All three
services must use this revision; the additive `auth.totp_last_step` migration
is applied by both runtimes. Existing data and enrollment remain intact.
See [the release audit](docs/release-2026-10-31.md) for evidence and limitations.
