#!/usr/bin/env python3
"""Seed a throwaway researchD DB for the researchR client integration run."""
import json, os, pathlib, sqlite3, sys

db = pathlib.Path(sys.argv[1]).resolve()
root = pathlib.Path(__file__).resolve().parents[2]
for suf in ("", "-wal", "-shm"):
    pathlib.Path(str(db) + suf).unlink(missing_ok=True)
db.parent.mkdir(parents=True, exist_ok=True)

con = sqlite3.connect(db)
con.executescript((root / "db" / "schema.sql").read_text())
con.execute("""INSERT INTO questions(id,label,sources,digest_mode,min_score,
                 from_days_back,email_to,delivery,study_filter)
               VALUES('seeded','Seeded question',?,?,?,?,?,?,?)""",
            (json.dumps({"pubmed": True}), "threshold", 1.25, 7,
             json.dumps(["me@example.org"]), json.dumps(["file"]), json.dumps([])))
con.execute("INSERT INTO concepts(question_id,name,position,terms,mesh) VALUES(?,?,?,?,?)",
            ("seeded", "drug", 0, json.dumps(["semaglutide"]), json.dumps([])))

RECS = [("doi:r1", "pubmed",    "10.1/1", "An RCT of semaglutide",      "rct",               0, 5.0),
        ("doi:r2", "europepmc", "10.1/2", "A meta-analysis of GLP-1",   "systematic_review", 0, 4.0),
        ("doi:r3", "biorxiv",   "10.1/3", "Preprint RCT of tirzepatide","rct",               1, 3.0),
        ("doi:r4", "crossref",  "10.1/4", "A low-scoring item",         "other",             0, 0.1)]
for k, src, doi, title, st, pp, score in RECS:
    con.execute("""INSERT INTO records(dedup_key,question_id,source,ext_id,doi,title,
                     abstract,authors,journal,pub_date,url,oa_url,type,study_type,
                     is_preprint,score,emailed)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
                (k, "seeded", src, k, doi, title, "An abstract.", "A; B", "J",
                 "2026-08-01", "https://doi.org/" + doi, "", "Journal Article",
                 st, pp, score))

SIGS = [("s1", "sec_filing",    "authoritative", 9.0, '{"form_type":"8-K"}'),
        ("s2", "press_release", "unverified",    2.0, "{}"),
        ("s3", "trial_result",  "reported",      5.0, '{"phase":"3"}')]
for k, st, conf, score, extra in SIGS:
    con.execute("""INSERT INTO signals(dedup_key,question_id,signal_type,source,ext_id,
                     title,summary,org,signal_date,url,confidence,extra,score,emailed)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
                (k, "seeded", st, "test", k, "Signal " + k, "Summary", "ACME",
                 "2026-08-10", "https://example.org/" + k, conf, extra, score))

con.execute("INSERT INTO signal_config(question_id) VALUES('seeded')")
for src, ok, n in [("pubmed", 1, 12), ("openalex", 1, 0), ("biorxiv", 0, 0)]:
    con.execute("INSERT INTO fetch_log(question_id,track,source,n_fetched,ok) VALUES(?,?,?,?,?)",
                ("seeded", "lit", src, n, ok))
con.commit(); con.close()
print(f"seeded {db}")
