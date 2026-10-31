#!/usr/bin/env python3
"""Prepare private first-use configuration without overwriting existing state."""
import argparse
import base64
import os
from pathlib import Path
import re
import secrets
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8000, help='Local web port (default: 8000)')
    parser.add_argument('--base-url', help='Public HTTPS origin; omit for localhost HTTP')
    parser.add_argument('--project', default='researchd', help='Distinct lowercase Compose project name')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('--port must be between 1 and 65535')
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', args.project):
        parser.error('--project must use lowercase letters, digits, underscores or hyphens')
    base_url = f'http://127.0.0.1:{args.port}'
    if args.base_url:
        parts = urlsplit(args.base_url)
        if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
                or parts.query or parts.fragment or parts.path not in ('', '/')
                or any(c.isspace() for c in args.base_url)):
            parser.error('--base-url must be an HTTPS origin without credentials, query or path')
        try:
            parts.port
        except ValueError:
            parser.error('--base-url has an invalid port')
        base_url = args.base_url.rstrip('/')
    root = Path(__file__).resolve().parents[1]
    values = {
        'APP_SECRET_KEY': base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        'API_TOKEN': secrets.token_hex(32),
        'WEB_BIND': '127.0.0.1', 'WEB_PORT': str(args.port),
        'COMPOSE_PROJECT_NAME': args.project, 'BASE_URL': base_url,
        'SECURE_COOKIE': '1' if args.base_url else '0',
        'RUN_ON_START': '0', 'SMTP_HOST': '', 'SMTP_USER': '',
        'SMTP_FROM': '', 'EMAIL_PW': '',
    }
    text = (root / '.env.example').read_text()
    for name, value in values.items():
        text, count = re.subn(rf'^{name}=.*$', lambda _: f'{name}={value}', text, flags=re.MULTILINE)
        if count != 1:
            raise SystemExit(f'Expected exactly one {name} in .env.example')
    for name in ('db', 'digests'):
        path = root / name
        if path.exists() and not path.is_dir():
            raise SystemExit(f'{name} must be a directory')
    try:
        fd = os.open(root / '.env', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit('.env already exists; it and its encryption key were left unchanged.')
    with os.fdopen(fd, 'w') as stream:
        stream.write(text)
    for name in ('db', 'digests'):
        (root / name).mkdir(exist_ok=True)
        (root / name).chmod(0o700)
    print('Created private .env (0600). Back up APP_SECRET_KEY separately from SQLite.')
    print('External delivery is disabled. Review settings and create your first question in the dashboard.')
    print('Start: docker compose up -d --build --wait')
    print(f'Setup: {base_url}/setup')


if __name__ == '__main__':
    main()
