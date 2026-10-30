#!/usr/bin/env bash
# ===========================================================================
# End-to-end: the researchR R client against a real, running researchD.
#
# Starts a throwaway uvicorn on a free port with a seeded scratch database,
# points the package's integration tests at it, and tears it down again.
# This is the only test that exercises both halves of the project together.
# ===========================================================================
set -euo pipefail
cd "$(dirname "$0")/../.."               # researchD/
RD="$PWD"

# researchR lives in its own repository. Point RESEARCHR_PKG at your checkout,
# or clone it beside this one and the default finds it.
PKG="${RESEARCHR_PKG:-$(dirname "$RD")/researchR}"
if [ ! -f "$PKG/DESCRIPTION" ]; then
  echo "researchR package not found at $PKG" >&2
  echo "clone it beside this repo, or set RESEARCHR_PKG:" >&2
  echo "  git clone https://github.com/CTTIR/researchR.git $(dirname "$RD")/researchR" >&2
  exit 2
fi

PY="$RD/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "Python test environment missing: $PY (create .venv and install requirements-test.txt first)" >&2
  exit 2
fi

SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/researchD-client.XXXXXX")"
DB="$SCRATCH/integration.sqlite"
TOKEN="integration-token-$$"
LOG="$SCRATCH/server.log"
PORT_FILE="$SCRATCH/port"
SERVER=""
cleanup() {
  local result=$?
  trap - EXIT
  if [ -n "$SERVER" ]; then
    kill "$SERVER" 2>/dev/null || true
    wait "$SERVER" 2>/dev/null || true
  fi
  if [ "$result" -ne 0 ] && [ -s "$LOG" ]; then
    echo "[integration] server log:" >&2
    cat "$LOG" >&2
  fi
  rm -rf "$SCRATCH"
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Both urllib and the R client's libcurl must contact this local fixture
# directly, even on workstations with HTTP(S)/ALL_PROXY configured.
export NO_PROXY="127.0.0.1,localhost,::1${NO_PROXY:+,$NO_PROXY}"
export no_proxy="127.0.0.1,localhost,::1${no_proxy:+,$no_proxy}"

"$PY" "$RD/dev/tests/seed_server.py" "$DB"

DB_PATH="$DB" \
APP_SECRET_KEY="$("$PY" -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')" \
API_TOKEN="$TOKEN" SECURE_COOKIE=0 \
"$PY" - "${PORT:-0}" "$PORT_FILE" > "$LOG" 2>&1 <<'PY' &
import os, socket, sys
from pathlib import Path
import uvicorn

# Reserve the OS-selected port until shutdown: selecting then closing a
# temporary socket would race other integration runs before uvicorn binds.
listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.bind(("127.0.0.1", int(sys.argv[1])))
listener.listen(128)
port = listener.getsockname()[1]
os.environ["BASE_URL"] = f"http://127.0.0.1:{port}"
Path(sys.argv[2]).write_text(str(port))
try:
    uvicorn.Server(uvicorn.Config("web.app:app", log_level="warning",
                                 timeout_graceful_shutdown=5)).run(sockets=[listener])
finally:
    listener.close()
PY
SERVER=$!

READY=0
for ((attempt=0; attempt<50; attempt++)); do
  if ! kill -0 "$SERVER" 2>/dev/null; then
    echo "[integration] researchD exited before becoming ready" >&2
    exit 1
  fi
  if [ -s "$PORT_FILE" ] && "$PY" - "$PORT_FILE" <<'PY' 2>/dev/null
import pathlib, sys, urllib.request
port = pathlib.Path(sys.argv[1]).read_text()
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open(f"http://127.0.0.1:{port}/healthz", timeout=0.5) as response:
    if response.status != 200 or response.read() != b"ok":
        raise RuntimeError("Unexpected readiness response")
PY
  then READY=1; break; fi
  sleep 0.2
done
if [ "$READY" -ne 1 ] || ! kill -0 "$SERVER" 2>/dev/null; then
  echo "[integration] researchD did not become ready within the startup timeout" >&2
  exit 1
fi
PORT="$(cat "$PORT_FILE")"

echo "[integration] researchD up on port $PORT; running the researchR suite"
cd "$PKG"
R_LIBS="$RD/dev/state/tmp/Rlib" \
RESEARCHR_TEST_URL="http://127.0.0.1:$PORT" \
RESEARCHR_TEST_TOKEN="$TOKEN" \
RESEARCHR_TEST_QUESTION="seeded" \
"$PY" - <<'PY'
import subprocess, sys
code = '''pkgload::load_all(".", quiet = TRUE);
          testthat::test_dir("tests/testthat", package = "researchR",
                            load_package = "none", reporter = "summary",
                            stop_on_failure = TRUE)'''
try:
    result = subprocess.run(["Rscript", "--vanilla", "-e", code], timeout=300)
except subprocess.TimeoutExpired:
    print("[integration] researchR suite exceeded its 300-second timeout", file=sys.stderr)
    sys.exit(1)
sys.exit(result.returncode)
PY
