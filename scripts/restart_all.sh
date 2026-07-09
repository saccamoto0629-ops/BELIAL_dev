#!/usr/bin/env bash
set -euo pipefail

SYSTEMCTL="${SYSTEMCTL:-systemctl}"

LONG_RUNNING_SERVICES=(
  "berial-news.service"
  "berial-ma-alert.service"
  "berial-macd-alert.service"
  "berial-bollinger-alert.service"
)

TIMER_UNITS=(
  "berial-chart-snapshot.timer"
)

for service in "${LONG_RUNNING_SERVICES[@]}"; do
  echo "Restarting ${service}"
  "${SYSTEMCTL}" restart "${service}"
done

for timer in "${TIMER_UNITS[@]}"; do
  echo "Restarting ${timer}"
  "${SYSTEMCTL}" restart "${timer}"
done

echo "Berial services restarted."
