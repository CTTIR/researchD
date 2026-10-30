#!/bin/sh
# ===========================================================================
# Disk-growth control. Both of the things that grow without bound here are
# outputs, not state: stored records and rendered digests.
#
#   scripts/prune.sh <db-path> <digests-dir> [record-months] [digest-keep]
#   scripts/prune.sh db/pipeline.sqlite digests 24 60
#
# Cron it monthly on the HOST:
#   0 4 1 * * /srv/researchD/scripts/prune.sh \
#             /srv/researchD/db/pipeline.sqlite /srv/researchD/digests
#
# What it does, and what it deliberately does NOT do:
#
#   records   pruned by first_seen older than <record-months> (default 24).
#             Pruning a record makes it eligible to be "new" again if the
#             same paper resurfaces in a later fetch window -- the dedup key
#             lives in this table and nowhere else. A source can re-index old
#             material, so rediscovery is possible even with a short lookback.
#             Records with unfinished file/Zotero deliveries are retained.
#   signals   same treatment, same reasoning.
#   fetch_log capped at the most recent 5000 rows. It is pure telemetry.
#   digests   the newest <digest-keep> HTML and Markdown files are kept.
#   WAL       checkpointed and truncated at the end.
#
#   questions, concepts, auth, sessions, signal_config, service_heartbeat are
#   never touched: they are configuration and identity, not accumulation.
# ===========================================================================
set -eu
DB="${1:?usage: prune.sh <db-path> <digests-dir> [record-months] [digest-keep]}"
DIGESTS="${2:?usage: prune.sh <db-path> <digests-dir> [record-months] [digest-keep]}"
MONTHS="${3:-24}"
KEEP="${4:-60}"

python3 - "$DB" "$DIGESTS" "$MONTHS" "$KEEP" <<'PY'
from pathlib import Path
import re
import sqlite3
import sys

def positive_integer(value, name):
    if not re.fullmatch(r"[0-9]+", value) or int(value) < 1:
        raise SystemExit(f"prune.sh: {name} must be a positive integer")
    return int(value)

# Validate the entire request before touching either records or files.
months = positive_integer(sys.argv[3], "record-months")
keep = positive_integer(sys.argv[4], "digest-keep")
db = Path(sys.argv[1]).resolve()
digests = Path(sys.argv[2])
if not db.is_file():
    raise SystemExit(f"prune.sh: no such database: {db}")
if digests.exists() and not digests.is_dir():
    raise SystemExit(f"prune.sh: digest path is not a directory: {digests}")
old_files = []
if digests.is_dir():
    for suffix in (".html", ".md", ".log"):
        files = [p for p in digests.iterdir()
                 if p.is_file() and p.suffix == suffix and p.resolve() != db]
        files.sort(key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)
        old_files.extend(files[keep:])

con = sqlite3.connect(db.as_uri() + "?mode=rw", uri=True, timeout=30)
try:
    con.execute("PRAGMA busy_timeout=30000;")
    con.execute("PRAGMA foreign_keys=ON;")
    with con:
        con.execute("BEGIN IMMEDIATE")
        cutoff = f"-{months} months"
        has_outbox = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='delivery_outbox'").fetchone()
        # Retry payloads are state. Completed entries cascade with their expired
        # records; unfinished entries, including any future retry state, remain.
        pending_guard = (" AND NOT EXISTS (SELECT 1 FROM delivery_outbox d WHERE "
                         "d.question_id=records.question_id AND d.dedup_key=records.dedup_key "
                         "AND d.state<>'sent')") if has_outbox else ""
        n_rec = con.execute("DELETE FROM records WHERE first_seen < datetime('now', ?)" + pending_guard,
                            (cutoff,)).rowcount
        n_sig = con.execute("DELETE FROM signals WHERE first_seen < datetime('now', ?)", (cutoff,)).rowcount
        n_log = con.execute("DELETE FROM fetch_log WHERE id NOT IN "
                            "(SELECT id FROM fetch_log ORDER BY id DESC LIMIT 5000)").rowcount
    con.execute("PRAGMA wal_checkpoint(TRUNCATE);")
finally:
    con.close()

# Pass whole paths to the filesystem, never through whitespace-delimited shell
# output. Spaces, quotes and newlines in a directory or filename are literal.
for path in old_files:
    path.unlink(missing_ok=True)
print(f"pruned {n_rec} record(s), {n_sig} signal(s), {n_log} fetch_log row(s) "
      f"older than {months} months")
if digests.is_dir():
    print(f"digests: kept the newest {keep} of each type in {digests}")
PY
