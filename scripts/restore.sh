#!/bin/sh
# ===========================================================================
# Restore a backup taken by scripts/backup.sh.
#
#   docker compose down            # nothing may be writing during a restore
#   scripts/restore.sh /var/backups/researchD/pipeline-20260826-030000.sqlite \
#                      db/pipeline.sqlite
#   docker compose up -d
#
# The current database and any WAL/SHM sidecars are preserved together, so a
# restore onto the wrong file is recoverable even after an unclean shutdown.
#
# Remember APP_SECRET_KEY must be the same value it was when the backup was
# taken, or the engine reports unreadable stored credentials and marks the
# affected questions as failed until their matching key is restored.
# ===========================================================================
set -eu
SRC="${1:?usage: restore.sh <backup-file> <db-path>}"
DST="${2:?usage: restore.sh <backup-file> <db-path>}"

python3 - "$SRC" "$DST" <<'PY'
from datetime import datetime
from pathlib import Path
import os
import shutil
import sqlite3
import sys
import tempfile

source = Path(sys.argv[1]).resolve()
destination = Path(sys.argv[2]).resolve()
if not source.is_file():
    raise SystemExit(f"restore.sh: no such backup: {source}")
if destination.exists() and not destination.is_file():
    raise SystemExit(f"restore.sh: destination is not a file: {destination}")
if source == destination or (destination.exists() and source.samefile(destination)):
    raise SystemExit("restore.sh: backup source and destination must be different files")
sidecars = [Path(str(destination) + suffix) for suffix in ("-wal", "-shm")]
if any(p.exists() and not p.is_file() for p in sidecars):
    raise SystemExit("restore.sh: a destination WAL/SHM path is not a file")
destination.parent.mkdir(parents=True, exist_ok=True)
fd, temporary = tempfile.mkstemp(prefix=".researchd-restore-", suffix=".tmp", dir=destination.parent)
os.close(fd)
temporary = Path(temporary)
try:
    # Stage a complete SQLite snapshot, including source WAL content if present.
    # A corrupt source cannot displace the current database.
    src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=30)
    staged = sqlite3.connect(temporary, timeout=30)
    try:
        try:
            src.backup(staged)
        except sqlite3.OperationalError as error:
            if (getattr(error, "sqlite_errorcode", None) != getattr(sqlite3, "SQLITE_CANTOPEN", 14)
                    and "unable to open database file" not in str(error).lower()):
                raise
            # SQLite must sometimes bootstrap missing WAL sidecars. Opening
            # the existing file in query-only mode allows that without SQL
            # writes or creation of a missing source database.
            src.close()
            src = sqlite3.connect(source.as_uri() + "?mode=rw", uri=True, timeout=30)
            src.execute("PRAGMA query_only=ON")
            src.backup(staged)
        staged.execute("PRAGMA journal_mode=DELETE")
        result = [r[0] for r in staged.execute("PRAGMA integrity_check")]
        if result != ["ok"]:
            raise RuntimeError("restore integrity check failed: " + "; ".join(result))
    finally:
        staged.close()
        src.close()

    saved = []
    if destination.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        fd, aside_name = tempfile.mkstemp(prefix=f"{destination.name}.replaced-{stamp}-",
                                          dir=destination.parent)
        os.close(fd)
        aside = Path(aside_name)
        # Copy the entire stopped database bundle before changing any part of it.
        # Raw preservation also works when the current DB itself is corrupt.
        shutil.copy2(destination, aside)
        for sidecar in sidecars:
            if sidecar.exists():
                copy = Path(str(aside) + str(sidecar)[len(str(destination)):])
                shutil.copy2(sidecar, copy)
                saved.append((sidecar, copy))
        os.chmod(temporary, destination.stat().st_mode & 0o777)
        print(f"existing database preserved: {aside}")
    else:
        # Without a main file, even stale sidecars can contain recovery evidence.
        # Require the operator to resolve them rather than silently discard them.
        if any(p.exists() for p in sidecars):
            raise RuntimeError("destination has WAL/SHM files but no database; preserve and review them before restoring")

    try:
        # Old WAL must never be applied to the newly restored main file. Its
        # recovery copy is already beside the preserved original database.
        for sidecar in sidecars:
            sidecar.unlink(missing_ok=True)
        os.replace(temporary, destination)
    except Exception:
        # If replacement fails, the old main file is still present. Put its
        # sidecars back so its committed WAL transactions remain readable.
        for original, copy in saved:
            shutil.copy2(copy, original)
        raise
finally:
    for suffix in ("", "-wal", "-shm"):
        Path(str(temporary) + suffix).unlink(missing_ok=True)

print(f"restored: {source} -> {destination}")
print("start the stack again with: docker compose up -d")
PY
