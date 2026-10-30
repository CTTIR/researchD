#!/usr/bin/env python3
"""Write only the scheduled engine's environment, safely quoted and private."""
import os
import shlex
import sys

KEYS = ('APP_SECRET_KEY', 'EMAIL_PW', 'ZOTERO_KEY', 'ENTREZ_KEY', 'OPENALEX_API_KEY',
        'TZ', 'CONTACT_EMAIL')


def write_environment(path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as stream:
        for key in KEYS:
            if key in os.environ:
                stream.write(f'export {key}={shlex.quote(os.environ[key])}\n')


if __name__ == '__main__':
    write_environment(sys.argv[1])
