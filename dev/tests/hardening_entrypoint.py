"""Run the real CLI twice with disabled sources and an ingested queued record."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[2]
with tempfile.TemporaryDirectory(prefix='researchd-entrypoint-') as tmp:
    target = Path(tmp)
    shutil.copytree(ROOT/'R',target/'R')
    (target/'web').mkdir()
    shutil.copy2(ROOT/'web/decrypt_key.py', target/'web/decrypt_key.py')
    (target/'db').mkdir();(target/'config').mkdir()
    shutil.copy2(ROOT/'db/schema.sql',target/'db/schema.sql')
    (target/'config/settings.yaml').write_text('''paths:
  db: db/pipeline.sqlite
  digests: digests
sources: {}
ranking:
  weight_recency: 1
  weight_source: 0.5
  weight_relevance: 2
  source_priority: {ingest: 1}
unpaywall: {email: ""}
zotero: {enabled: false}
''')
    con = sqlite3.connect(target/'db/pipeline.sqlite')
    con.executescript((target/'db/schema.sql').read_text())
    con.close()
    env = dict(os.environ,R_LIBS=os.environ.get('R_LIBS',str(ROOT/'dev/state/tmp/Rlib')),RUN_MODE='lit',
               PATH=str(Path(sys.executable).parent)+os.pathsep+os.environ.get('PATH',''))
    command = ['Rscript','--vanilla',str(target/'R/run_all.R')]
    # A fresh installation is healthy while waiting for its first question.
    (target/'config/questions.yaml').write_text('questions: []\n')
    idle = subprocess.run(command,cwd=target,env=env,capture_output=True,text=True,timeout=45)
    assert idle.returncode == 0 and 'Idle: no enabled questions' in idle.stderr, idle.stdout+idle.stderr
    con = sqlite3.connect(target/'db/pipeline.sqlite')
    hb = con.execute("SELECT last_ok,last_detail,last_error FROM service_heartbeat WHERE service='pipeline'").fetchone()
    assert hb[0] and 'idle: no enabled questions' in hb[1] and not hb[2], hb
    con.close()
    missing = subprocess.run(command+['missing'],cwd=target,env=env,capture_output=True,text=True,timeout=45)
    assert missing.returncode != 0 and 'No matching enabled questions' in missing.stderr
    con = sqlite3.connect(target/'db/pipeline.sqlite')
    con.execute("INSERT INTO questions(id,label,delivery) VALUES('test','Synthetic','[\"file\"]')")
    con.execute("INSERT INTO concepts(question_id,name,terms) VALUES('test','topic','[\"alpha\"]')")
    con.execute("INSERT INTO records(dedup_key,question_id,source,title,study_type,score)"
                " VALUES('synthetic','test','ingest','Alpha study','unclassified',1)")
    con.execute("INSERT INTO delivery_outbox(channel,question_id,dedup_key) VALUES('file','test','synthetic')")
    con.commit();con.close()
    env = dict(os.environ,R_LIBS=os.environ.get('R_LIBS',str(ROOT/'dev/state/tmp/Rlib')),RUN_MODE='lit',
               PATH=str(Path(sys.executable).parent)+os.pathsep+os.environ.get('PATH',''))
    command = ['Rscript','--vanilla',str(target/'R/run_all.R')]
    for cycle in range(2):
        run = subprocess.run(command,cwd=target,env=env,capture_output=True,text=True,timeout=45)
        assert run.returncode == 0, run.stdout+run.stderr
        assert len(list((target/'digests').glob('*.html'))) == 1
    con = sqlite3.connect(target/'db/pipeline.sqlite')
    assert con.execute("SELECT state FROM delivery_outbox").fetchone()[0] == 'sent'
    assert con.execute("SELECT last_ok FROM service_heartbeat WHERE service='pipeline'").fetchone()[0]
    con.close()
    bad = subprocess.run(command,cwd=target,env={**env,'RUN_MODE':'invalid'},
                         capture_output=True,text=True,timeout=30)
    assert bad.returncode != 0 and 'RUN_MODE must be' in bad.stderr
    # Full and signal-only runs share one process lock with literature runs.
    ready = target/'lock-ready'
    holder = subprocess.Popen(['Rscript','--vanilla','-e',
        'lock <- filelock::lock("db/pipeline.sqlite.pipeline.lock", timeout=0); '
        'stopifnot(!is.null(lock)); file.create("lock-ready"); Sys.sleep(30)'],
        cwd=target,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    try:
        import time
        deadline=time.monotonic()+15
        while not ready.exists() and time.monotonic()<deadline and holder.poll() is None:
            time.sleep(0.05)
        assert ready.exists(), 'lock holder did not start'
        blocked=subprocess.run(command,cwd=target,env={**env,'RUN_MODE':'signals'},
                               capture_output=True,text=True,timeout=15)
        assert blocked.returncode != 0 and 'Another pipeline run is active' in blocked.stderr
    finally:
        holder.terminate(); holder.communicate(timeout=10)
    resumed=subprocess.run(command,cwd=target,env={**env,'RUN_MODE':'all'},
                           capture_output=True,text=True,timeout=45)
    assert resumed.returncode==0, resumed.stdout+resumed.stderr

    # Unloadable questions must degrade the heartbeat without starving a valid
    # question or leaking encrypted values/parser snippets into diagnostics.
    master = Fernet.generate_key().decode()
    wrong = Fernet.generate_key().decode()
    plain = 'synthetic-sensitive-upstream-key'
    encrypted = Fernet(master.encode()).encrypt(plain.encode()).decode()
    sentinel = '2000-01-01 00:00:00'
    con = sqlite3.connect(target/'db/pipeline.sqlite')
    con.execute("INSERT INTO questions(id,label,delivery) VALUES('bad_json','Malformed','[]')")
    con.execute("INSERT INTO concepts(question_id,name,terms) VALUES('bad_json','topic',?)",
                ('{private-parser-canary',))
    con.execute("INSERT INTO questions(id,label,delivery,api_keys_enc) VALUES('bad_key','Unreadable','[]',?)",
                (json.dumps({'pubmed': encrypted}),))
    con.execute("INSERT INTO records(dedup_key,question_id,source,title,study_type,score)"
                " VALUES('valid_after_failure','test','ingest','Alpha continuation','unclassified',1)")
    con.execute("INSERT INTO delivery_outbox(channel,question_id,dedup_key) VALUES('file','test','valid_after_failure')")
    con.execute("UPDATE service_heartbeat SET last_ok=? WHERE service='pipeline'", (sentinel,))
    con.commit(); con.close()
    mixed = subprocess.run(command,cwd=target,env={**env,'APP_SECRET_KEY':wrong},
                           capture_output=True,text=True,timeout=45)
    assert mixed.returncode != 0, mixed.stdout+mixed.stderr
    assert 'bad_json' in mixed.stderr and 'bad_key' in mixed.stderr
    assert '=== Synthetic (test)' in mixed.stderr
    con = sqlite3.connect(target/'db/pipeline.sqlite')
    hb = con.execute("SELECT last_ok,last_error FROM service_heartbeat WHERE service='pipeline'").fetchone()
    assert hb[0] == sentinel and 'bad_json' in hb[1] and 'bad_key' in hb[1], hb
    assert 'Invalid JSON in concepts.terms' in hb[1] and 'Cannot decrypt configured API key' in hb[1]
    assert con.execute("SELECT state FROM delivery_outbox WHERE dedup_key='valid_after_failure'").fetchone()[0] == 'sent'
    con.close()
    diagnostics = mixed.stdout + mixed.stderr + hb[1]
    assert all(secret not in diagnostics for secret in (master, wrong, encrypted, plain, 'private-parser-canary'))

    # Selection happens before loading: an unrelated broken key cannot degrade
    # an explicitly requested valid question or leak an irrelevant warning.
    selected = subprocess.run(command+['test'],cwd=target,env={**env,'APP_SECRET_KEY':wrong},
                              capture_output=True,text=True,timeout=45)
    assert selected.returncode == 0 and 'skipping unloadable' not in selected.stderr, selected.stdout+selected.stderr
    con = sqlite3.connect(target/'db/pipeline.sqlite')
    assert con.execute("SELECT last_error FROM service_heartbeat WHERE service='pipeline'").fetchone()[0] == ''
    con.close()
    for qid, secret_key, expected in [
        ('bad_json',wrong,'Invalid JSON in concepts.terms'),
        ('bad_key',wrong,'Cannot decrypt configured API key'),
        ('bad_key','','APP_SECRET_KEY is missing')]:
        con = sqlite3.connect(target/'db/pipeline.sqlite')
        con.execute("UPDATE service_heartbeat SET last_ok=? WHERE service='pipeline'", (sentinel,))
        con.commit(); con.close()
        failed = subprocess.run(command+[qid],cwd=target,env={**env,'APP_SECRET_KEY':secret_key},
                                capture_output=True,text=True,timeout=45)
        assert failed.returncode != 0 and expected in failed.stderr, failed.stdout+failed.stderr
        con = sqlite3.connect(target/'db/pipeline.sqlite')
        hb = con.execute("SELECT last_ok,last_error FROM service_heartbeat WHERE service='pipeline'").fetchone()
        con.close()
        assert hb[0] == sentinel and qid in hb[1] and expected in hb[1], hb
    repaired = subprocess.run(command+['bad_key'],cwd=target,env={**env,'APP_SECRET_KEY':master},
                              capture_output=True,text=True,timeout=45)
    assert repaired.returncode == 0 and plain not in repaired.stdout+repaired.stderr, repaired.stdout+repaired.stderr

    # Signals credentials fail inside the question boundary, while another
    # selected question still runs. Literature-only runs need no signal key.
    con = sqlite3.connect(target/'db/pipeline.sqlite')
    con.execute("INSERT INTO signal_config(question_id,sources,api_keys_enc) VALUES('test','{}',?)",
                (json.dumps({'openfda': encrypted}),))
    con.execute("INSERT INTO questions(id,label,delivery) VALUES('other','Other valid','[]')")
    con.commit(); con.close()
    signal_failure = subprocess.run(command+['test','other'],cwd=target,
        env={**env,'APP_SECRET_KEY':wrong,'RUN_MODE':'all'},capture_output=True,text=True,timeout=45)
    assert signal_failure.returncode != 0 and 'Cannot decrypt configured API key' in signal_failure.stderr
    assert '=== Other valid (other)' in signal_failure.stderr
    assert all(secret not in signal_failure.stdout+signal_failure.stderr for secret in (master,wrong,encrypted,plain))
    literature_only = subprocess.run(command+['test'],cwd=target,
        env={**env,'APP_SECRET_KEY':wrong},capture_output=True,text=True,timeout=45)
    assert literature_only.returncode == 0, literature_only.stdout+literature_only.stderr
print('PASS: real CLI, queued export, repeat/lock safety, failed-load heartbeat and selection, secret-safe credential failures and recovery')
