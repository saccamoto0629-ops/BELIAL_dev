#!/bin/sh
# POSIX sh (no bash arrays): restart every Berial resident service + timer.
#
# Issue #41 review (Major 4): one unit failing to restart (e.g. a not-yet-deployed
# berial-paper-signal.service) must NOT abort the remaining restarts. Each unit is
# attempted; failures are reported and the script exits non-zero at the end, so a
# missing/broken unit is visible without silently skipping the units after it.
set -eu

SYSTEMCTL="${SYSTEMCTL:-systemctl}"

# Space-separated unit lists, restarted in this order.
LONG_RUNNING_SERVICES="berial-news.service berial-ma-alert.service berial-macd-alert.service berial-bollinger-alert.service berial-paper-signal.service"
TIMER_UNITS="berial-chart-snapshot.timer"

rc=0

for service in $LONG_RUNNING_SERVICES; do
  echo "Restarting ${service}"
  if ! "${SYSTEMCTL}" restart "${service}"; then
    echo "WARNING: failed to restart ${service}" >&2
    rc=1
  fi
done

for timer in $TIMER_UNITS; do
  echo "Restarting ${timer}"
  if ! "${SYSTEMCTL}" restart "${timer}"; then
    echo "WARNING: failed to restart ${timer}" >&2
    rc=1
  fi
done

if [ "${rc}" -ne 0 ]; then
  echo "Berial services restart completed with failures." >&2
  exit "${rc}"
fi

echo "Berial services restarted."
