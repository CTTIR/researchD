#!/usr/bin/env bash
# ===========================================================================
# Every check the build phases ran, in one command.
#
#   dev/tests/run_all.sh            # offline suite
#   dev/tests/run_all.sh --live     # also hit the real external APIs and
#                                   # run the researchR client against a
#                                   # throwaway server
# ===========================================================================
set -uo pipefail
cd "$(dirname "$0")/../.."
PY=.venv/bin/python
pass=0; fail=0; failed=()

run() {
  local label="$1"; shift
  printf '%-34s ' "$label"
  if "$@" > /tmp/researchd-test.$$ 2>&1; then
    echo "PASS"; pass=$((pass + 1))
  else
    echo "FAIL"; fail=$((fail + 1)); failed+=("$label")
    sed 's/^/      /' /tmp/researchd-test.$$ | tail -12
  fi
  rm -f /tmp/researchd-test.$$
}

echo "── R engine ──────────────────────────────────────"
run "parse every engine source"  dev/tests/Rrun.sh dev/tests/parse_all.R
run "date parsing"               dev/tests/Rrun.sh dev/tests/parse_date.R
run "classifier edge cases"      dev/tests/Rrun.sh dev/tests/classify_table.R
run "dedup and ranking"          dev/tests/Rrun.sh dev/tests/dedup_rank.R
run "fetch plumbing + decrypt"   dev/tests/Rrun.sh dev/tests/fetch_plumbing.R
run "engine hardening"          dev/tests/Rrun.sh dev/tests/hardening_engine.R
run "Zotero failure and retry"  dev/tests/Rrun.sh dev/tests/hardening_delivery.R
run "nullable delivery metadata" dev/tests/Rrun.sh dev/tests/hardening_engine_delivery.R

echo
echo "── database ──────────────────────────────────────"
run "SQL vs schema coverage"     $PY dev/tests/schema_coverage.py .
run "WAL, contention, cascade"   $PY dev/tests/db_phase2.py
run "schema parity + migrations" $PY dev/tests/db_phase2_migrate.py

echo
echo "── services ──────────────────────────────────────"
run "taxonomy sync (A2)"         $PY dev/tests/taxonomy_sync.py
run "web: feeds, auth, api/v1"   $PY dev/tests/web_phase4.py
run "scheduler: cadence, sends"  $PY dev/tests/scheduler_phase5.py
run "integration: all services"  $PY dev/tests/integration_phase6.py
run "security: adversarial"      $PY dev/tests/security_phase7.py
run "ops: restart, backup, prune" $PY dev/tests/ops_phase8.py
run "web security regressions"   $PY dev/tests/hardening_web.py
run "scheduler regressions"      $PY dev/tests/hardening_scheduler.py
run "dashboard DOM"              node dev/tests/hardening_web_dom.js
run "cron environment handoff"   $PY dev/tests/hardening_cron.py
run "real pipeline entrypoint"   $PY dev/tests/hardening_entrypoint.py
run "delivery-aware retention"   $PY dev/tests/hardening_retention.py

echo
echo "── audit probes ──────────────────────────────────"
run "ingest parity, alerts, digest" $PY dev/tests/audit_correctness.py
run "deps, docs, env, WAL bound"    $PY dev/tests/audit_completeness.py

if [ "${1:-}" = "--live" ]; then
  echo
  echo "── live (network) ────────────────────────────────"
  run "external APIs"            dev/tests/Rrun.sh dev/tests/live_fetchers.R
  run "researchR client e2e"     dev/tests/integration_client.sh
fi

echo
echo "═════════════════════════════════════════════════"
echo "$pass passed, $fail failed"
if [ "$fail" -gt 0 ]; then printf '  failed: %s\n' "${failed[@]}"; exit 1; fi
