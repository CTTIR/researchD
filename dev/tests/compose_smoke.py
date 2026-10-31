#!/usr/bin/env python3
"""Exercise an isolated Compose stack. Refuses any nonempty database."""
import json
from pathlib import Path
import sqlite3
import subprocess
import urllib.request
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
values = dict(line.split('=', 1) for line in (ROOT/'.env').read_text().splitlines()
              if line and not line.startswith('#') and '=' in line)
url = values['BASE_URL']
assert url.startswith('http://127.0.0.1:'), 'Use a local disposable stack only'
database = ROOT/'db/pipeline.sqlite'
with sqlite3.connect(database) as con:
    for table in ('auth', 'questions', 'records', 'signals'):
        assert con.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0, f'{table} must be empty'

def api(path, payload=None, token=None):
    headers = {'Authorization': 'Bearer '+(token or values['API_TOKEN'])}
    if payload is not None:
        headers['Content-Type'] = 'application/json'
    request = urllib.request.Request(url+path, headers=headers,
        data=None if payload is None else json.dumps(payload).encode())
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)

sources = dict.fromkeys(('pubmed','europepmc','openalex','semanticscholar','crossref','clinicaltrials','biorxiv'), False)
api('/api/v1/questions', {'id':'release_smoke','label':'Synthetic release smoke',
    'concepts':[{'name':'topic','terms':['synthetic']}], 'sources':sources,
    'delivery':['file'], 'email_enabled':False})
with sqlite3.connect(database) as con:
    # Local-only fixture; production ingestion is separately tested over HTTP.
    con.execute("INSERT INTO records(dedup_key,question_id,source,title,study_type,score) VALUES('smoke','release_smoke','synthetic','Synthetic <paper> & data','unclassified',1)")
    con.execute("INSERT INTO delivery_outbox(channel,question_id,dedup_key) VALUES('file','release_smoke','smoke')")
for _ in range(2):
    subprocess.run(['docker','compose','exec','-T','pipeline','env','-i',
        'PATH=/usr/local/bin:/usr/bin:/bin','/bin/sh','-c',
        '. /app/.cron_env; RUN_MODE=lit Rscript R/run_all.R release_smoke'], cwd=ROOT,check=True)
with sqlite3.connect(database) as con:
    assert con.execute('SELECT state FROM delivery_outbox').fetchone()[0]=='sent'
    assert con.execute("SELECT last_ok FROM service_heartbeat WHERE service='pipeline'").fetchone()[0]
assert len(list((ROOT/'digests').glob('*.html')))==1
records = api('/api/v1/records?question_id=release_smoke')
assert records['total']==1 and records['records'][0]['title']=='Synthetic <paper> & data'
with urllib.request.urlopen(url+'/feed/release_smoke/all.xml',timeout=15) as response:
    feed=ET.fromstring(response.read())
assert feed.find('.//item/title').text=='Synthetic <paper> & data'

# Exercise SMTP over an actual local socket inside the scheduler container.
# The server accepts only this synthetic message and has no forwarding path.
smtp_test = r'''
import socketserver, threading, sqlite3, json, tempfile
from pathlib import Path
from web import scheduler as s
# Clone the synthetic shared state so the normal scheduler tick cannot race
# this controlled send or acknowledge its rows. The real SMTP socket stays local.
temporary = tempfile.TemporaryDirectory(prefix='researchd-smtp-')
clone = str(Path(temporary.name) / 'state.sqlite')
with sqlite3.connect(s.DB_PATH) as source, sqlite3.connect(clone) as target:
    source.backup(target)
s.DB_PATH = clone
messages=[]
class Capture(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b'220 local-test\r\n')
        while True:
            line=self.rfile.readline()
            if not line: return
            command=line.split(b' ',1)[0].strip().upper()
            if command==b'DATA':
                self.wfile.write(b'354 End with dot\r\n'); chunks=[]
                while True:
                    line=self.rfile.readline()
                    if line==b'.\r\n': break
                    if not line: raise RuntimeError('incomplete SMTP data')
                    chunks.append(line)
                messages.append(b''.join(chunks));self.wfile.write(b'250 accepted\r\n')
            elif command==b'QUIT':
                self.wfile.write(b'221 goodbye\r\n');return
            else: self.wfile.write(b'250 local-test\r\n')
with socketserver.TCPServer(('127.0.0.1',0),Capture) as server:
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    s.SMTP_HOST='127.0.0.1';s.SMTP_PORT=server.server_address[1];s.SMTP_TLS='none'
    s.SMTP_USER='';s.SMTP_FROM='test@example.invalid'
    con=s.db()
    con.execute("UPDATE questions SET email_enabled=1,email_to=?,email_cron='* * * * *',email_last_sent='2000-01-01T00:00:00+00:00' WHERE id='release_smoke'",(json.dumps(['recipient@example.invalid']),))
    con.commit();con.close()
    assert s.run_once() is True
    assert len(messages)==1
    assert s.run_once() is True and len(messages)==1
    con=s.db();assert con.execute('SELECT emailed FROM records').fetchone()[0]==1
    con.execute("UPDATE questions SET email_enabled=0,email_to='[]'");con.commit();con.close()
    server.shutdown();thread.join(timeout=5)
temporary.cleanup()
print('PASS: real SMTP socket, successful acknowledgment, no duplicate on repeat')
'''
subprocess.run(['docker','compose','exec','-T','scheduler','python3','-'],input=smtp_test,
               text=True,cwd=ROOT,check=True,timeout=90)
print('PASS: Compose cron environment, repeat-safe file export, heartbeat, authenticated API, RSS and SMTP capture')
