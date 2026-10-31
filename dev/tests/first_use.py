"""Bootstrap preserves credentials and yields usable local/HTTPS configuration."""
import base64
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root/'scripts').mkdir()
    shutil.copy2(ROOT/'scripts/setup.py', root/'scripts/setup.py')
    shutil.copy2(ROOT/'.env.example', root/'.env.example')
    command = [sys.executable, str(root/'scripts/setup.py')]
    run = subprocess.run(command + ['--port', '18084'], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    envfile = root/'.env'
    original = envfile.read_bytes()
    values = dict(line.split('=', 1) for line in original.decode().splitlines() if '=' in line and not line.startswith('#'))
    assert len(base64.urlsafe_b64decode(values['APP_SECRET_KEY'])) == 32
    assert len(values['API_TOKEN']) == 64 and values['API_TOKEN'] not in run.stdout
    assert values['BASE_URL'] == 'http://127.0.0.1:18084' and values['SECURE_COOKIE'] == '0'
    assert not values['SMTP_HOST'] and not values['EMAIL_PW']
    assert envfile.stat().st_mode & 0o777 == 0o600
    assert (root/'db').is_dir() and (root/'digests').is_dir()
    again = subprocess.run(command, capture_output=True, text=True)
    assert again.returncode != 0 and envfile.read_bytes() == original
    envfile.rename(root/'saved.env')
    bad = subprocess.run(command + ['--base-url', 'http://remote.invalid'], capture_output=True)
    assert bad.returncode != 0 and not envfile.exists()
    good = subprocess.run(command + ['--base-url', 'https://feeds.example.org'], capture_output=True, text=True)
    assert good.returncode == 0, good.stderr
    assert 'SECURE_COOKIE=1' in envfile.read_text()
    assert 'BASE_URL=https://feeds.example.org' in envfile.read_text()
print('PASS: safe first-use setup, private secrets, existing-state preservation, HTTPS validation')
