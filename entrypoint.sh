#!/usr/bin/env bash
# ============================================================
# entrypoint.sh
# Cron does NOT inherit container env vars. So we:
#   1. dump the current environment to a file cron jobs can source
#   2. render the crontab from a template (schedule from $CRON_SCHEDULE)
#   3. run cron in the foreground so the container stays alive
#      and logs stream to `docker logs`.
# Set RUN_ON_START=1 to also run once immediately at boot.
# ============================================================
set -euo pipefail

SCHEDULE="${CRON_SCHEDULE:-0 6 * * *}"          # full run: daily 06:00
FAST_SCHEDULE="${CRON_FAST_SCHEDULE:-15 * * * *}" # signals lane: hourly at :15

# 1. persist env so the cron job can source it (filter to what we need).
# APP_SECRET_KEY must be here: cron jobs inherit nothing, and without it
# configured credentials cannot be decrypted and the affected questions fail
# visibly. Export the matching key so scheduled runs can authenticate.
# The file holds secrets -> create it 0600 before anything is written.
umask 077
: > /app/.cron_env
# Quote every value as shell data, including spaces, quotes and newlines. Plain
# printenv output is not shell syntax and unexported assignments never reach R.
python3 /app/scripts/write_cron_env.py /app/.cron_env
chmod 600 /app/.cron_env
umask 022

# 2. render crontab: run wrapper, sourcing env, logging to stdout (PID 1)
sed -e "s|__SCHEDULE__|${SCHEDULE}|" -e "s|__FAST_SCHEDULE__|${FAST_SCHEDULE}|" /app/crontab.template > /etc/cron.d/pipeline
echo "" >> /etc/cron.d/pipeline   # cron requires trailing newline
chmod 0644 /etc/cron.d/pipeline
# cron refuses files not owned by root. Already true when running as root
# (the usual case); tolerate failure so `set -e` cannot kill the container
# in a rootless/user-namespace deployment where the file is already ours.
chown root:root /etc/cron.d/pipeline 2>/dev/null || true
# NOTE: do NOT also `crontab /etc/cron.d/pipeline`. The template carries the
# 6th "user" field that /etc/cron.d requires; a per-user crontab must not
# have it, so installing the same file both ways makes cron read "root" as
# the command and fail every cycle.

echo "[entrypoint] schedule: ${SCHEDULE}  (TZ=${TZ:-UTC})"

# 3. optional immediate run (handy for first-run / testing)
if [ "${RUN_ON_START:-0}" = "1" ]; then
  echo "[entrypoint] RUN_ON_START=1 -> running once now"
  . /app/.cron_env
  Rscript /app/R/run_all.R || echo "[entrypoint] initial run exited non-zero"
fi

# 4. cron in foreground; -f keeps it attached, logs go to container stdout
echo "[entrypoint] starting cron in foreground"
exec cron -f
