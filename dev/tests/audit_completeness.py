#!/usr/bin/env python3
"""Audit pass — completeness and the two reliability probes not covered by a
phase test (WAL bound; documentation/reference integrity)."""
import ast, os, pathlib, re, sqlite3, sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
# researchR is a separate repository. Its probes run when a checkout is
# reachable and are skipped, loudly, when it is not.
PKG = pathlib.Path(os.environ.get("RESEARCHR_PKG", ROOT.parent / "researchR"))
TMP = ROOT / "dev" / "state" / "tmp"; TMP.mkdir(parents=True, exist_ok=True)
fails = []
def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if (detail and not ok) else ""))
    if not ok: fails.append(name)

compose = (ROOT / "docker-compose.yml").read_text()
envex = (ROOT / ".env.example").read_text()
readme = (ROOT / "README.md").read_text()

# ------------------------------------------------------ 2/3. env var parity
print("2/3. environment variables")
compose_refs = set(re.findall(r"\$\{([A-Z_0-9]+)", compose))
documented = set(re.findall(r"^([A-Z_0-9]+)=", envex, re.M))
check("every compose ${VAR} is documented in .env.example",
      not (compose_refs - documented), str(sorted(compose_refs - documented)))
check("nothing documented in .env.example is unused by compose",
      not (documented - compose_refs), str(sorted(documented - compose_refs)))

# What do the services actually READ?
read_by_code = set()
for pyf in (ROOT / "web").glob("*.py"):
    read_by_code |= set(re.findall(r'os\.environ(?:\.get)?\(\s*"([A-Z_0-9]+)"', pyf.read_text()))
    read_by_code |= set(re.findall(r'os\.getenv\(\s*"([A-Z_0-9]+)"', pyf.read_text()))
rtext = "\n".join((ROOT / "R" / f).read_text() for f in os.listdir(ROOT / "R") if f.endswith(".R"))
read_by_code |= set(re.findall(r'Sys\.getenv\(\s*"([A-Za-z_0-9]+)"', rtext))
for sh in ("entrypoint.sh", "cron.sh", "crontab.template"):
    read_by_code |= set(re.findall(r"\$\{?([A-Z_][A-Z_0-9]*)", (ROOT / sh).read_text()))
# names the code sets rather than consumes, or that compose supplies directly
INTERNAL = {"DB_PATH", "PATH", "HOME", "RUN_MODE", "SCHEDULE", "FAST_SCHEDULE",
            "STAMP", "DIR", "DB", "LOG", "TZ"}
# Variables belonging to the local test harness and to the researchR client,
# NOT to the deployed services. .env.example documents what docker-compose
# reads; putting these there would tell an operator to set something the
# stack never looks at.
DEV_ONLY = {"RESEARCHR_PKG", "RESEARCHR_URL", "RESEARCHR_TOKEN",
            "RESEARCHR_TEST_URL", "RESEARCHR_TEST_TOKEN",
            "RESEARCHR_TEST_QUESTION", "RESEARCHR_SKIP_LIVE", "R_LIBS", "PORT"}
undocumented = {v for v in read_by_code - documented - INTERNAL - DEV_ONLY
                if v.isupper()}
check("no service reads an undocumented environment variable",
      not undocumented, str(sorted(undocumented)))
check("DB_PATH is supplied by compose even though it is not user-facing",
      'DB_PATH: "/app/db/pipeline.sqlite"' in compose)

# ------------------------------------------------- 4/5. deps vs Dockerfiles
print("\n4/5. declared dependencies vs what the code imports")
STDLIB = set(sys.stdlib_module_names)
webdeps = (ROOT / "Dockerfile.web").read_text() + (ROOT / "requirements-web.txt").read_text()
schdeps = (ROOT / "Dockerfile.scheduler").read_text() + (ROOT / "requirements-scheduler.txt").read_text()

def third_party(path):
    mods = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.add(node.module.split(".")[0])
    return {m for m in mods if m not in STDLIB and m != "web"}

for f, dockerfile, name in [("app.py", webdeps, "Dockerfile.web"),
                            ("security.py", webdeps, "Dockerfile.web"),
                            ("decrypt_key.py", (ROOT / "Dockerfile").read_text(), "Dockerfile"),
                            ("scheduler.py", schdeps, "Dockerfile.scheduler")]:
    mods = third_party(ROOT / "web" / f)
    missing = {m for m in mods if m.lower() not in dockerfile.lower()}
    check(f"every third-party import in web/{f} is installed by {name}",
          not missing, f"missing: {sorted(missing)} (imports: {sorted(mods)})")

r_ns = set(re.findall(r"\b([A-Za-z][A-Za-z0-9._]*)::", rtext)) - {"stats", "utils", "base"}
dockerfile = (ROOT / "Dockerfile").read_text()
missing_r = {p for p in r_ns if p not in dockerfile}
check("every R namespace used via :: is installed by the Dockerfile",
      not missing_r, f"missing: {sorted(missing_r)}")
check("python3 + cryptography are installed for the R->python decrypt seam",
      "python3-cryptography" in dockerfile)

# REQUIRED_PKGS drives the bare-R install path documented in the README
req = re.search(r"REQUIRED_PKGS <- c\((.*?)\)", rtext, re.S)
declared = set(re.findall(r'"([A-Za-z0-9._]+)"', req.group(1))) if req else set()
check("the bare-R REQUIRED_PKGS list covers every namespace actually used",
      not (r_ns - declared), f"used but not listed: {sorted(r_ns - declared)}")
check("REQUIRED_PKGS lists nothing the code no longer uses",
      not (declared - r_ns), f"listed but unused: {sorted(declared - r_ns)}")

# ------------------------------------------------------ 7/8. doc integrity
print("\n7/8. documentation and reference integrity")
for var in sorted(re.findall(r"`([A-Z_0-9]{3,})`", readme)):
    if var in documented or var in INTERNAL or var in DEV_ONLY \
       or var in {"MAX", "OK", "TODO"}:
        continue
    check(f"README mentions {var}, which is documented in .env.example",
          False, f"{var} is not in .env.example and is not a known dev-only name")

referenced = set()
for doc in ("README.md",):
    text = (ROOT / doc).read_text()
    referenced |= {(doc, m) for m in re.findall(
        r"`((?:dev/|R/|web/|db/|config/|scripts/)[A-Za-z0-9_./-]+)`", text)}
# db/*.sqlite is created on first run, not shipped, so a reference to it is
# correct even though the file does not exist in a clean checkout.
RUNTIME = re.compile(r"^db/.*\.sqlite$")
missing_refs = [f"{doc} -> {path}" for doc, path in referenced
                if not (ROOT / path).exists() and not RUNTIME.match(path)]
check("every repo path referenced in README exists",
      not missing_refs, "; ".join(sorted(missing_refs)))

script_refs = set(re.findall(r"scripts/([a-z_]+\.sh)", readme))
check("every script the README tells you to run exists and is executable",
      all((ROOT / "scripts" / s).exists() and os.access(ROOT / "scripts" / s, os.X_OK)
          for s in script_refs), str(sorted(script_refs)))
test_refs = set(re.findall(r"dev/tests/([A-Za-z0-9_]+\.(?:py|R|sh))", readme))
check("every test the README lists exists",
      all((ROOT / "dev" / "tests" / t).exists() for t in test_refs),
      str(sorted(t for t in test_refs if not (ROOT / "dev" / "tests" / t).exists())))

# The client README must not promise functions the package does not export.
if (PKG / "NAMESPACE").exists():
    exported = set(re.findall(r"export\((\w+)\)", (PKG / "NAMESPACE").read_text()))
    promised = set(re.findall(r"`(rr_[a-z_]+)\(", (PKG / "README.md").read_text()))
    check("every researchR function named in its README is exported",
          not (promised - exported), str(sorted(promised - exported)))
    check("every exported researchR function is documented in a man page",
          all((PKG / "man" / f"{fn}.Rd").exists() for fn in exported),
          str(sorted(fn for fn in exported if not (PKG / "man" / f"{fn}.Rd").exists())))
else:
    print(f"  [SKIP] researchR client probes — no checkout at {PKG} "
          f"(set RESEARCHR_PKG or clone it beside this repo)")

# ------------------------------------------------------- reliability #9 WAL
print("\nreliability 9. the WAL stays bounded")
db = TMP / "wal.sqlite"
for suf in ("", "-wal", "-shm"):
    pathlib.Path(str(db) + suf).unlink(missing_ok=True)
con = sqlite3.connect(db)
con.executescript((ROOT / "db" / "schema.sql").read_text())
con.execute("INSERT INTO questions(id,label) VALUES('q','Q')")
con.commit()
for i in range(4000):
    con.execute("INSERT INTO records(dedup_key,question_id,title,abstract) VALUES(?,?,?,?)",
                (f"k{i}", "q", "T" * 200, "A" * 800))
    if i % 250 == 0:
        con.commit()
con.commit()
wal = pathlib.Path(str(db) + "-wal")
grew = wal.stat().st_size if wal.exists() else 0
con.execute("PRAGMA wal_checkpoint(TRUNCATE);")
after = wal.stat().st_size if wal.exists() else 0
con.close()
check("the WAL does grow under sustained writes (so this probe is meaningful)",
      grew > 0, f"{grew} bytes")
check("a TRUNCATE checkpoint bounds it back to zero",
      after == 0, f"{grew} -> {after} bytes")
check("the engine performs that checkpoint every run",
      "wal_checkpoint(TRUNCATE)" in (ROOT / "R" / "run_all.R").read_text())

print()
if fails:
    print(f"FAILED: {len(fails)} check(s):"); [print("   -", f) for f in fails]; sys.exit(1)
print("ALL AUDIT COMPLETENESS PROBES PASSED"); sys.exit(0)
