#!/bin/sh
# ===========================================================================
# WAL-safe SQLite backup.
#
# A plain `cp` of a live WAL database can capture a torn state: the main file
# and the -wal file are two files, and copying them at different instants can
# produce a snapshot that never existed. SQLite's own backup API takes a
# consistent snapshot of a database that is being written to.
#
# Usage:
#   scripts/backup.sh <db-path> <backup-dir> [keep]
#   scripts/backup.sh db/pipeline.sqlite /var/backups/researchd 14
#
# Cron it on the HOST (the container has no scheduler for this):
#   0 3 * * * /srv/researchd/scripts/backup.sh \
#             /srv/researchd/db/pipeline.sqlite /var/backups/researchd
#
# ALSO BACK UP APP_SECRET_KEY, separately and somewhere else. It is not in
# the database. Without it, the per-question API keys in a restored backup
# are ciphertext you cannot read -- you would have to re-enter every one.
# ===========================================================================
set -eu

DB="${1:?usage: backup.sh <db-path> <backup-dir> [keep]}"
DIR="${2:?usage: backup.sh <db-path> <backup-dir> [keep]}"
KEEP="${3:-14}"

# Python's SQLite backup API also handles quotes and URI punctuation in paths.
python3 - "$DB" "$DIR" "$KEEP" <<'PY'
from datetime import datetime
from pathlib import Path
import os
import re
import sqlite3
import sys
import tempfile

if not re.fullmatch(r"[0-9]+", sys.argv[3]) or int(sys.argv[3]) < 1:
    raise SystemExit("backup.sh: keep must be a positive integer")
keep = int(sys.argv[3])
source = Path(sys.argv[1]).resolve()
directory = Path(sys.argv[2])
if not source.is_file():
    raise SystemExit(f"backup.sh: no such database: {source}")
directory.mkdir(parents=True, exist_ok=True)
stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
fd, temporary = tempfile.mkstemp(prefix=f".pipeline-{stamp}-", suffix=".tmp", dir=directory)
os.close(fd)
temporary = Path(temporary)
output = temporary.with_name(temporary.name[1:-4] + ".sqlite")
try:
    src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(temporary, timeout=30)
    try:
        try:
            src.backup(dst)
        except sqlite3.OperationalError as error:
            if (getattr(error, "sqlite_errorcode", None) != getattr(sqlite3, "SQLITE_CANTOPEN", 14)
                    and "unable to open database file" not in str(error).lower()):
                raise
            # A checkpointed WAL-mode file may have no sidecars yet. SQLite
            # needs a writable handle to create them, even for a read-only
            # backup. Query-only mode prevents logical writes; mode=rw cannot
            # accidentally create a missing source database.
            src.close()
            src = sqlite3.connect(source.as_uri() + "?mode=rw", uri=True, timeout=30)
            src.execute("PRAGMA query_only=ON")
            src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
        result = [r[0] for r in dst.execute("PRAGMA integrity_check")]
        if result != ["ok"]:
            raise RuntimeError("backup integrity check failed: " + "; ".join(result))
    finally:
        dst.close()
        src.close()
    # Only complete verified snapshots participate in retention. Random suffixes
    # preserve distinct snapshots even when two backups start in the same second.
    os.replace(temporary, output)
finally:
    for suffix in ("", "-wal", "-shm"):
        Path(str(temporary) + suffix).unlink(missing_ok=True)

snapshots = [p for p in directory.glob("pipeline-*.sqlite")
             if p.is_file() and p.resolve() not in (source, output.resolve())]
snapshots.sort(key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)
for old in snapshots[keep - 1:]:
    old.unlink(missing_ok=True)
print(f"backup ok: {output} ({output.stat().st_size} bytes, integrity_check=ok)")
PY
