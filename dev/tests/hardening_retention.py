"""Exercise retention and recovery on disposable databases and literal paths."""
from pathlib import Path
from contextlib import contextmanager
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def open_db(path):
    con = sqlite3.connect(path)
    try:
        with con:
            yield con
    finally:
        con.close()


def run_script(name, *args, success=True):
    result = subprocess.run([str(ROOT / "scripts" / name), *map(str, args)],
                            capture_output=True, text=True)
    if success and result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    return result


def values(path):
    with open_db(path) as con:
        return [r[0] for r in con.execute("SELECT value FROM evidence ORDER BY value")]


def simple_db(path, value):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE evidence(value TEXT)")
    con.execute("INSERT INTO evidence VALUES(?)", (value,))
    con.commit()
    con.close()


def leave_committed_wal(path, value):
    # Simulate an unclean exit AFTER commit; no writer remains during restore.
    subprocess.run([sys.executable, "-c", """
import os, sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute('PRAGMA journal_mode=WAL')
con.execute('PRAGMA wal_autocheckpoint=0')
con.execute('INSERT INTO evidence VALUES(?)', (sys.argv[2],))
con.commit()
os._exit(0)
""", str(path), value], check=True)


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def populated_db(self):
        path = self.root / "db.sqlite"
        con = sqlite3.connect(path)
        con.executescript((ROOT / "db/schema.sql").read_text())
        con.execute("INSERT INTO questions(id,label) VALUES('q','Q')")
        for key, channel, state in [
            ("pending_file", "file", "pending"),
            ("pending_zotero", "zotero", "pending"),
            ("completed", "file", "sent"),
            ("completed_zotero", "zotero", "sent"),
        ]:
            con.execute("INSERT INTO records(dedup_key,question_id,title,first_seen) "
                        "VALUES(?,'q',?,'2000-01-01')", (key, key))
            con.execute("INSERT INTO delivery_outbox(channel,question_id,dedup_key,state) "
                        "VALUES(?,'q',?,?)", (channel, key, state))
        con.commit()
        con.close()
        return path

    def test_pending_payloads_survive_and_completed_entries_cascade(self):
        path = self.populated_db()
        run_script("prune.sh", path, self.root / "digests", 24, 60)
        with open_db(path) as con:
            expected = {("pending_file",), ("pending_zotero",)}
            self.assertEqual(set(con.execute("SELECT dedup_key FROM records")), expected)
            self.assertEqual(set(con.execute("SELECT dedup_key FROM delivery_outbox")), expected)
            self.assertEqual(list(con.execute("PRAGMA foreign_key_check")), [])
            con.execute("INSERT INTO records(dedup_key,question_id,title) VALUES('completed','q','New appearance')")
            con.execute("INSERT INTO delivery_outbox(channel,question_id,dedup_key) VALUES('file','q','completed')")
            self.assertEqual(con.execute("SELECT state FROM delivery_outbox WHERE dedup_key='completed'").fetchone()[0], "pending")

    def test_invalid_retention_values_make_no_changes(self):
        path = self.populated_db()
        digests = self.root / "digests"
        digests.mkdir()
        for name in ("first.html", "second.html"):
            (digests / name).write_text(name)
        for months, keep in [(0, 1), (-1, 1), (24, 0), (24, -1), (24, "invalid")]:
            with self.subTest(months=months, keep=keep):
                result = run_script("prune.sh", path, digests, months, keep, success=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("positive integer", result.stderr)
                with open_db(path) as con:
                    self.assertEqual(con.execute("SELECT COUNT(*) FROM records").fetchone()[0], 4)
                self.assertEqual(len(list(digests.iterdir())), 2)
        backups = self.root / "backups"
        for keep in (0, -1, "invalid"):
            result = run_script("backup.sh", path, backups, keep, success=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(backups.exists())

    def test_digest_rotation_uses_literal_paths(self):
        path = self.populated_db()
        sentinel = self.root / "digest"
        sentinel.write_text("outside the selected directory")
        digests = self.root / "digest dir"
        digests.mkdir()
        for extension, old_name in [("html", "old report"), ("md", "old 'quoted'"), ("log", "old\nline")]:
            old = digests / f"{old_name}.{extension}"
            new = digests / f"newest.{extension}"
            old.write_text("old")
            new.write_text("new")
            os.utime(old, (10, 10))
            os.utime(new, (20, 20))
        run_script("prune.sh", path, digests, 24, 1)
        self.assertEqual({p.name for p in digests.iterdir()}, {"newest.html", "newest.md", "newest.log"})
        self.assertEqual(sentinel.read_text(), "outside the selected directory")

    def test_backups_are_distinct_verified_and_safe_with_special_paths(self):
        source = self.root / "source ?#' data.sqlite"
        simple_db(source, "first")
        directory = self.root / "backup dir's #copy"
        snapshots = []
        for value in ("first", "second", "third"):
            with open_db(source) as con:
                con.execute("DELETE FROM evidence")
                con.execute("INSERT INTO evidence VALUES(?)", (value,))
            output = run_script("backup.sh", source, directory, 3)
            self.assertIn("integrity_check=ok", output.stdout)
            current = set(directory.glob("pipeline-*.sqlite"))
            new = current - set(snapshots)
            self.assertEqual(len(new), 1)
            snapshot = new.pop()
            snapshots.append(snapshot)
            self.assertEqual(values(snapshot), [value])
        self.assertEqual([values(p) for p in snapshots], [["first"], ["second"], ["third"]])
        self.assertFalse(list(directory.glob("*.tmp")))

    def test_backup_rotation_preserves_live_source_and_new_snapshot(self):
        directory = self.root / "backup dir"
        directory.mkdir()
        sentinel = self.root / "backup"
        sentinel.write_text("outside")
        source = directory / "pipeline-live.sqlite"
        simple_db(source, "live")
        old = directory / "pipeline-old snapshot.sqlite"
        simple_db(old, "old")
        os.utime(old, (2100000000, 2100000000))
        run_script("backup.sh", source, directory, 1)
        self.assertEqual(values(source), ["live"])
        snapshots = [p for p in directory.glob("pipeline-*.sqlite") if p != source]
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(values(snapshots[0]), ["live"])
        self.assertFalse(old.exists())
        self.assertEqual(sentinel.read_text(), "outside")

    def test_live_backup_contains_committed_wal_but_not_uncommitted_work(self):
        source = self.root / "live.sqlite"
        simple_db(source, "base")
        writer = sqlite3.connect(source)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO evidence VALUES('committed')")
        writer.commit()
        writer.execute("INSERT INTO evidence VALUES('not committed')")
        backups = self.root / "backups"
        run_script("backup.sh", source, backups, 1)
        snapshot = next(backups.glob("*.sqlite"))
        self.assertEqual(values(snapshot), ["base", "committed"])
        writer.rollback()

    def test_failed_backup_never_rotates_valid_snapshots(self):
        source = self.root / "source.sqlite"
        simple_db(source, "safe")
        directory = self.root / "backups"
        run_script("backup.sh", source, directory, 1)
        snapshot = next(directory.glob("*.sqlite"))
        before = snapshot.read_bytes()
        source.write_text("not a database")
        result = run_script("backup.sh", source, directory, 1, success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(snapshot.read_bytes(), before)
        self.assertEqual(list(directory.iterdir()), [snapshot])

    def test_checkpointed_wal_database_without_sidecars_can_be_copied(self):
        source = self.root / "wal-without-sidecars.sqlite"
        simple_db(source, "checkpointed")
        con = sqlite3.connect(source)
        con.execute("PRAGMA journal_mode=WAL")
        con.close()
        self.assertFalse(Path(str(source) + "-wal").exists())
        backups = self.root / "backups"
        run_script("backup.sh", source, backups, 1)
        self.assertEqual(values(next(backups.glob("*.sqlite"))), ["checkpointed"])
        # Recreate the no-sidecar state independently for the restore path.
        con = sqlite3.connect(source)
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()
        target = self.root / "restored.sqlite"
        run_script("restore.sh", source, target)
        self.assertEqual(values(target), ["checkpointed"])

    def test_restore_preserves_committed_old_wal_for_recovery(self):
        live = self.root / "live.sqlite"
        simple_db(live, "old base")
        leave_committed_wal(live, "committed pending work")
        wal = Path(str(live) + "-wal")
        original_wal = wal.read_bytes()
        source = self.root / "replacement.sqlite"
        simple_db(source, "restored")
        run_script("restore.sh", source, live)
        preserved = [p for p in self.root.glob("live.sqlite.replaced-*")
                     if not p.name.endswith(("-wal", "-shm"))]
        self.assertEqual(len(preserved), 1)
        self.assertEqual(Path(str(preserved[0]) + "-wal").read_bytes(), original_wal)
        self.assertEqual(values(preserved[0]), ["committed pending work", "old base"])
        self.assertEqual(values(live), ["restored"])
        self.assertFalse(wal.exists())

    def test_restore_rejects_corrupt_or_identical_source_without_displacing_data(self):
        live = self.root / "live.sqlite"
        simple_db(live, "original")
        before = live.read_bytes()
        corrupt = self.root / "corrupt.sqlite"
        corrupt.write_text("corrupt")
        for source in (corrupt, live):
            result = run_script("restore.sh", source, live, success=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(live.read_bytes(), before)
            self.assertEqual(values(live), ["original"])
        self.assertFalse(list(self.root.glob("live.sqlite.replaced-*")))

    def test_restore_includes_source_wal_and_can_replace_a_corrupt_destination(self):
        source = self.root / "backup #with? wal.sqlite"
        simple_db(source, "source base")
        leave_committed_wal(source, "source committed")
        live = self.root / "live.sqlite"
        live.write_bytes(b"corrupt original kept for recovery")
        run_script("restore.sh", source, live)
        self.assertEqual(values(live), ["source base", "source committed"])
        preserved = next(self.root.glob("live.sqlite.replaced-*"))
        self.assertEqual(preserved.read_bytes(), b"corrupt original kept for recovery")


if __name__ == "__main__":
    unittest.main()
