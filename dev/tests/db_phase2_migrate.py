#!/usr/bin/env python3
"""Phase 2: R vs Python schema equivalence, and additive-migration safety."""
import json, os, pathlib, re, sqlite3, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
TMP = ROOT / "dev" / "state" / "tmp"
SCHEMA = (ROOT / "db" / "schema.sql").read_text()
fails = []

def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        fails.append(name)

def colmap(con):
    tabs = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {t: sorted(r[1] for r in con.execute(f"PRAGMA table_info({t})")) for t in sorted(tabs)}

print("1. the R engine and the web service build the SAME schema")
p = TMP / "p2_py.sqlite"
for suf in ("", "-wal", "-shm"):
    pathlib.Path(str(p) + suf).unlink(missing_ok=True)
con = sqlite3.connect(p); con.executescript(SCHEMA)
py = colmap(con); con.close()
# Regenerate the R-side column map rather than trusting a cached one: it is
# derived from schema.sql, so a stale file makes this test fail for the wrong
# reason (and, worse, could make it pass for the wrong reason).
gen = subprocess.run([str(ROOT / "dev" / "tests" / "Rrun.sh"),
                      "dev/tests/db_phase2_r.R"], cwd=ROOT,
                     capture_output=True, text=True)
check("the R-side schema map regenerated", gen.returncode == 0,
      (gen.stderr or "")[-300:])
r = json.loads((TMP / "p2_r_cols.json").read_text())
r = {k: sorted(v) for k, v in r.items()}
check("same table set", set(py) == set(r), f"py-only={sorted(set(py)-set(r))} r-only={sorted(set(r)-set(py))}")
diffs = {t: {"py_only": sorted(set(py[t]) - set(r.get(t, []))),
             "r_only": sorted(set(r.get(t, [])) - set(py[t]))}
         for t in py if set(py[t]) != set(r.get(t, []))}
check("same columns in every table", not diffs, json.dumps(diffs))

print("\n2. additive migration onto an older deployed DB")
# Build a DB that predates the migration columns, as a real older install would.
MIGRATED = [("questions", "digest_mode"), ("questions", "min_score"),
            ("signal_config", "feed_categories"), ("signal_config", "instant_alerts"),
            ("records", "oa_url"), ("records", "provenance"), ("auth", "totp_last_step")]
old = SCHEMA
old = re.sub(r"CREATE TABLE IF NOT EXISTS delivery_outbox \(.*?\n\);\n", "", old, flags=re.S)
old = re.sub(r"CREATE INDEX IF NOT EXISTS idx_delivery_pending[^\n]*\n", "", old)
for _tab, col in MIGRATED:
    old = re.sub(rf"^\s*{col}\s+[^\n]*\n", "", old, flags=re.M)
p = TMP / "p2_old.sqlite"
for suf in ("", "-wal", "-shm"):
    pathlib.Path(str(p) + suf).unlink(missing_ok=True)
con = sqlite3.connect(p); con.executescript(old)
before = colmap(con)
missing_before = [(t, c) for t, c in MIGRATED if c not in before[t]]
check("the simulated old DB really lacks the new columns",
      len(missing_before) == len(MIGRATED), f"{missing_before}")
# seed a row so we can prove data survives the migration
con.execute("INSERT INTO questions(id,label) VALUES('legacy','Legacy question')")
con.commit(); con.close()

env = dict(os.environ, DB_PATH=str(p), APP_SECRET_KEY="x" * 32, SECURE_COOKIE="0")
out = subprocess.run(
    [str(ROOT / ".venv" / "bin" / "python"), "-c",
     "from web.app import init_schema; init_schema(); print('migrated')"],
    cwd=ROOT, env=env, capture_output=True, text=True)
check("web/app.py init_schema() runs against the old DB",
      out.returncode == 0 and "migrated" in out.stdout, (out.stderr or "").strip()[-300:])

con = sqlite3.connect(p)
after = colmap(con)
still = [(t, c) for t, c in MIGRATED if c not in after[t]]
check("every migration column was added", not still, f"{still}")
rows = con.execute("SELECT id,label FROM questions").fetchall()
check("pre-existing data survived", rows == [("legacy", "Legacy question")], str(rows))
check("the additive delivery queue is created without replaying historical data",
      con.execute("SELECT COUNT(*) FROM delivery_outbox").fetchone()[0] == 0)
# and the migration is itself idempotent
out2 = subprocess.run(
    [str(ROOT / ".venv" / "bin" / "python"), "-c",
     "from web.app import init_schema; init_schema(); init_schema(); print('twice')"],
    cwd=ROOT, env=env, capture_output=True, text=True)
check("running init_schema twice more is a no-op",
      out2.returncode == 0 and "twice" in out2.stdout, (out2.stderr or "").strip()[-300:])
con.close()

print("\n3. the R engine carries the SAME migration list")
import ast
tree = ast.parse((ROOT / "web" / "app.py").read_text())
py_migr = None
for node in ast.walk(tree):
    if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "_MIGRATIONS" for t in node.targets):
        py_migr = [tuple(x) for x in ast.literal_eval(node.value)]
rsrc = (ROOT / "R" / "07_db_questions.R").read_text()
block = re.search(r"DB_MIGRATIONS <- list\((.*?)\n\)", rsrc, re.S).group(1)
r_migr = [tuple(x.strip().strip('"') for x in m)
          for m in re.findall(r'list\(\s*("[^"]*")\s*,\s*("[^"]*")\s*,\s*("[^"]*")\s*\)', block)]
check("R DB_MIGRATIONS matches web/app.py _MIGRATIONS",
      py_migr == r_migr, f"py={py_migr}\n        r ={r_migr}")

print("\n4. the R engine migrates an old DB on its own (it starts first)")
p2 = TMP / "p2_old_r.sqlite"
for suf in ("", "-wal", "-shm"):
    pathlib.Path(str(p2) + suf).unlink(missing_ok=True)
con = sqlite3.connect(p2); con.executescript(old)
con.execute("INSERT INTO questions(id,label) VALUES('legacy','Legacy question')")
con.commit(); con.close()
outR = subprocess.run([str(ROOT / "dev" / "tests" / "Rrun.sh"), "-e",
                       f'source("R/00_utils.R"); source("R/07_db_questions.R"); '
                       f'con <- DBI::dbConnect(RSQLite::SQLite(), "{p2}"); '
                       f'db_init_schema(con, "db/schema.sql"); cat("ok\\n")'],
                      cwd=ROOT, capture_output=True, text=True)
check("R db_init_schema() succeeds on the old DB", outR.returncode == 0,
      (outR.stderr or "").strip()[-300:])
con = sqlite3.connect(p2); afterR = colmap(con)
stillR = [(t, c) for t, c in MIGRATED if c not in afterR[t]]
check("R added every migration column", not stillR, f"{stillR}")
rowsR = con.execute("SELECT id,label FROM questions").fetchall()
check("pre-existing data survived the R migration",
      rowsR == [("legacy", "Legacy question")], str(rowsR))
con.close()

print("\n5. the R engine tolerates a DB that only app.py has migrated")
# R applies schema.sql but has no _MIGRATIONS table of its own; it must not
# fail on a DB where app.py already added the columns.
out3 = subprocess.run([str(ROOT / "dev" / "tests" / "Rrun.sh"), "-e",
                       f'source("R/00_utils.R"); source("R/07_db_questions.R"); '
                       f'con <- DBI::dbConnect(RSQLite::SQLite(), "{p}"); '
                       f'db_init_schema(con, "db/schema.sql"); '
                       f'cat(nrow(DBI::dbGetQuery(con, "SELECT * FROM questions")), "\\n")'],
                      cwd=ROOT, capture_output=True, text=True)
check("R db_init_schema() runs on the migrated DB",
      out3.returncode == 0, (out3.stderr or "").strip()[-300:])

print()
if fails:
    print(f"FAILED: {len(fails)} check(s): " + "; ".join(fails)); sys.exit(1)
print("ALL PHASE 2 MIGRATION CHECKS PASSED"); sys.exit(0)
