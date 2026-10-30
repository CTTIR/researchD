#!/usr/bin/env python3
# ============================================================
# decrypt_key.py — decrypt one Fernet token from stdin, print plaintext.
# Used by the R engine to recover per-question API keys at fetch time.
# Reads APP_SECRET_KEY from the environment. Failures return a nonzero status
# and a fixed diagnostic; neither ciphertext nor key material is printed.
#   echo "<token>" | python3 decrypt_key.py
# ============================================================
import os, sys

token = sys.stdin.read().strip()
if not token:
    sys.exit(0)
key = os.environ.get("APP_SECRET_KEY", "")
if not key:
    sys.stderr.write("APP_SECRET_KEY is required for stored credentials.\n")
    sys.exit(2)
try:
    from cryptography.fernet import Fernet
except Exception:
    sys.stderr.write("The Python cryptography dependency is unavailable.\n")
    sys.exit(3)

try:
    sys.stdout.write(Fernet(key.encode()).decrypt(token.encode()).decode())
except Exception:
    sys.stderr.write("Stored credential could not be decrypted; verify the matching master key.\n")
    sys.exit(4)
