"""Cron children receive exactly the configured values without shell evaluation."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / 'cron_env'
    values = {'APP_SECRET_KEY': 'synthetic-key',
              'ZOTERO_KEY': "spaces 'quotes' $literal; $(touch unwanted)\nsecond line",
              'TZ': 'Europe/Berlin', 'CONTACT_EMAIL': 'audit@example.invalid'}
    env = {'PATH': os.environ['PATH'], **values, 'UNRELATED_SECRET': 'must-not-copy'}
    subprocess.run([sys.executable, str(ROOT/'scripts/write_cron_env.py'), str(path)],
                   env=env, check=True)
    code = 'import os,json; print(json.dumps(dict(os.environ)))'
    result = subprocess.run(['/bin/sh','-c','. "$1"; exec "$2" -c "$3"',
                             'sh',str(path),sys.executable,code],
                            cwd=tmp, env={'PATH':os.environ['PATH']},
                            capture_output=True,text=True,check=True)
    inherited = json.loads(result.stdout)
    assert all(inherited.get(key) == value for key,value in values.items())
    assert 'UNRELATED_SECRET' not in inherited
    assert not (Path(tmp)/'unwanted').exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
print('PASS: cron exports, quoting, allowlist, and private file mode')
