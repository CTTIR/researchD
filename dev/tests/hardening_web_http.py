#!/usr/bin/env python3
"""Real local HTTP authentication/privacy checks using disposable credentials/state."""
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

import httpx
import pyotp
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[2]


def main():
    with tempfile.TemporaryDirectory(prefix="researchd-http-") as temp:
        path = Path(temp)
        log_path = path / "server.log"
        db_path = path / "state.sqlite"
        with socket.socket() as listener, log_path.open("w+") as log:
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
            base = f"http://127.0.0.1:{listener.getsockname()[1]}"
            env = dict(os.environ, DB_PATH=str(db_path), BASE_URL=base,
                       APP_SECRET_KEY=Fernet.generate_key().decode(), SECURE_COOKIE="0",
                       API_TOKEN="synthetic-api", INGEST_TOKEN="synthetic-ingest")
            process = subprocess.Popen([sys.executable, "-m", "uvicorn", "web.app:app",
                                        "--fd", str(listener.fileno()), "--log-level", "warning"],
                                       cwd=ROOT, env=env, pass_fds=(listener.fileno(),),
                                       stdout=log, stderr=log)
            try:
                with httpx.Client(base_url=base, trust_env=False, timeout=10) as owner, \
                        httpx.Client(base_url=base, trust_env=False, timeout=10) as fresh:
                    for _ in range(100):
                        if process.poll() is not None:
                            raise RuntimeError("HTTP fixture exited before startup")
                        try:
                            if owner.get("/healthz", timeout=0.2).status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        time.sleep(0.1)
                    else:
                        raise RuntimeError("HTTP fixture startup timed out")
                    credentials = {"username": "synthetic-admin", "password": "synthetic-long-password"}
                    setup = {**credentials, "password2": credentials["password"]}
                    assert owner.post("/setup", data=setup,
                                      headers={"Origin": "https://evil.invalid"}).status_code == 403
                    assert owner.post("/setup", data=setup, headers={"Origin": base}).status_code == 303
                    assert owner.post("/login", data=credentials, headers={"Origin": base}).status_code == 303
                    page = owner.get("/2fa/enroll")
                    secret = re.search(r"<code>([A-Z2-7]+)</code>", page.text).group(1)
                    enrollment_code = pyotp.TOTP(secret).now()
                    enrolled = owner.post("/2fa/enroll", data={"code": enrollment_code})
                    recovery = re.findall(r"\b(?:[0-9a-f]{4}-){3}[0-9a-f]{4}\b", enrolled.text)
                    assert len(recovery) == 8
                    assert owner.get("/").status_code == 200
                    fresh.post("/login", data=credentials)
                    assert fresh.get("/2fa/enroll").headers["location"] == "/2fa"
                    assert fresh.post("/2fa/enroll", data={"code": enrollment_code}).status_code == 409
                    assert fresh.post("/2fa", data={"code": enrollment_code}).status_code == 200
                    assert fresh.get("/").status_code == 307
                    with sqlite3.connect(db_path) as con:
                        last_step = con.execute("SELECT totp_last_step FROM auth WHERE id=1").fetchone()[0]
                    next_step = max(last_step + 1, int(time.time()) // 30)
                    fresh.post("/2fa", data={"code": pyotp.TOTP(secret).at(next_step * 30)})
                    assert fresh.get("/").status_code == 200
                    fresh.post("/logout")
                    fresh.post("/login", data=credentials)
                    fresh.post("/2fa", data={"code": recovery[0]})
                    assert fresh.get("/").status_code == 200
                    # Recovery permits access, but cannot silently replace the factor.
                    assert fresh.post("/2fa/enroll", data={"code": recovery[0]}).status_code == 409
                    fresh.post("/logout")
                    fresh.post("/login", data=credentials)
                    fresh.post("/2fa", data={"code": recovery[0]})
                    assert fresh.get("/").status_code == 307
                    question = {"id": "privacy", "label": "Privacy", "concepts": [{"name": "topic", "terms": ["test"]}]}
                    headers = {"Authorization": "Bearer synthetic-api"}
                    assert owner.post("/api/v1/questions", headers=headers, json=question).status_code == 200
                    api_secret = "synthetic-api-key-not-for-errors"
                    for route, body in [
                        ("/api/v1/questions", {**question, "from_days_back": 0, "api_keys": {"pubmed": api_secret}}),
                        ("/api/v1/signal_config/privacy", {"min_confidence": "invalid", "api_keys": {"openfda": api_secret}}),
                        ("/api/v1/signal_config/privacy", {"api_keys": {api_secret: 1}}),
                    ]:
                        response = owner.post(route, headers=headers, json=body)
                        assert response.status_code == 422
                        assert api_secret not in response.text
                        assert all(set(error) == {"loc", "type", "msg"} for error in response.json()["detail"])
                    print("Real HTTP: setup origin, MFA replay, next OTP, single-use recovery, enrollment lock and validation privacy passed")
            except Exception:
                log.flush()
                print(log_path.read_text(), file=sys.stderr)
                raise
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    main()
