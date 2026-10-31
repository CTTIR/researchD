# ============================================================
# web/security.py
# Crypto + auth primitives, shared by the web app.
#   - Fernet (AES) encryption for per-question API keys at rest
#   - bcrypt password + recovery-code hashing
#   - TOTP (pyotp) for 2FA
#   - server-side session tokens
#
# Master key comes from env APP_SECRET_KEY (a urlsafe base64 32-byte
# Fernet key). Generate one with:
#     python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# ============================================================
import os, json, secrets, time, math
from collections import OrderedDict
from threading import Lock
from datetime import datetime, timedelta, timezone

import bcrypt
import pyotp
from cryptography.fernet import Fernet, InvalidToken

# ---- master key / Fernet -------------------------------------
_KEY = os.environ.get("APP_SECRET_KEY", "")

def _fernet() -> Fernet:
    if not _KEY:
        raise RuntimeError("APP_SECRET_KEY not set; cannot encrypt/decrypt API keys.")
    try:
        return Fernet(_KEY.encode() if isinstance(_KEY, str) else _KEY)
    except Exception as e:
        raise RuntimeError(f"APP_SECRET_KEY is not a valid Fernet key: {e}")

def encrypt_secret(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode()).decode()

def decrypt_secret(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return ""   # wrong/rotated key -> treat as absent rather than crash

def encrypt_keymap(d: dict) -> str:
    """Encrypt each value of a {source: key} dict; return JSON of {source: token}."""
    return json.dumps({k: encrypt_secret(v) for k, v in (d or {}).items() if v})

def decrypt_keymap(s: str) -> dict:
    try:
        raw = json.loads(s or "{}")
    except json.JSONDecodeError:
        return {}
    return {k: decrypt_secret(v) for k, v in raw.items()}

# ---- passwords -----------------------------------------------
def hash_pw(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()

def verify_pw(pw: str, pw_hash: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), pw_hash.encode())
    except Exception:
        return False

# ---- recovery codes ------------------------------------------
def gen_recovery_codes(n: int = 8) -> list[str]:
    # human-friendly: 4 groups of 4 hex chars
    return ["-".join(secrets.token_hex(2) for _ in range(4)) for _ in range(n)]

def hash_codes(codes: list[str]) -> list[str]:
    return [hash_pw(c) for c in codes]

def consume_recovery_code(code: str, hashed_json: str):
    """Return (ok, new_hashed_json) — removes the code if it matched."""
    try:
        hashes = json.loads(hashed_json or "[]")
    except json.JSONDecodeError:
        return False, hashed_json
    for h in hashes:
        if verify_pw(code.strip(), h):
            hashes.remove(h)
            return True, json.dumps(hashes)
    return False, hashed_json

# ---- TOTP ----------------------------------------------------
def new_totp_secret() -> str:
    return pyotp.random_base32()

def totp_uri(secret: str, username: str, issuer: str = "ResearchFeeds") -> str:
    return pyotp.totp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)

def verify_totp(secret: str, code: str) -> bool:
    if not secret or not code:
        return False
    # valid_window=1 tolerates ~30s clock drift each way
    return pyotp.TOTP(secret).verify(code.strip().replace(" ", ""), valid_window=1)


def unused_totp_step(secret: str, code: str, last_step: int = -1):
    """Return an unused time step within the allowed drift, or None.

    The caller must persist it in the same transaction that authenticates the
    session. Enrollment consumes its confirmation code as well (RFC 6238).
    """
    if not secret or not code:
        return None
    code = code.strip().replace(" ", "")
    if len(code) != 6 or not code.isascii() or not code.isdigit():
        return None
    totp = pyotp.TOTP(secret)
    current = int(time.time()) // totp.interval
    matches = [step for step in range(max(0, current - 1), current + 2)
               if pyotp.utils.strings_equal(totp.generate_otp(step), code)]
    step = max(matches) if matches else None
    return step if step is not None and step > (last_step if last_step is not None else -1) else None

# ---- sessions ------------------------------------------------
def new_session_token() -> str:
    return secrets.token_urlsafe(32)

def session_expiry(hours: int = 12) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()

def is_expired(expires_at_iso: str) -> bool:
    try:
        return datetime.fromisoformat(expires_at_iso) < datetime.now(timezone.utc)
    except Exception:
        return True


class AttemptLimiter:
    """Bounded, thread-safe rate limit for a single web worker.

    Successful attempts count too: possessing a password must not let a client
    repeatedly create fresh sessions and reset the second-factor budget.
    State expires naturally and is intentionally reset on service restart.
    """
    def __init__(self, limit=20, window_seconds=300, max_clients=2048):
        self.limit = limit
        self.window = window_seconds
        self.max_clients = max_clients
        self._attempts = OrderedDict()
        self._lock = Lock()

    def retry_after(self, key):
        now = time.monotonic()
        with self._lock:
            start, count = self._attempts.pop(key, (now, 0))
            if now - start >= self.window:
                start, count = now, 0
            wait = max(1, math.ceil(self.window - (now - start))) if count >= self.limit else 0
            self._attempts[key] = (start, count + (not wait))
            while len(self._attempts) > self.max_clients:
                self._attempts.popitem(last=False)
            return wait
