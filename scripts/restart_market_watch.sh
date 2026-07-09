#!/usr/bin/env bash
set -euo pipefail

SYSTEMCTL="${SYSTEMCTL:-systemctl}"

MARKET_WATCH_SERVICES=(
  "berial-ma-alert.service"
  "berial-macd-alert.service"
  "berial-bollinger-alert.service"
)

for service in "${MARKET_WATCH_SERVICES[@]}"; do
  echo "Restarting ${service}"
  "${SYSTEMCTL}" restart "${service}"
done

echo "Berial Market Watch alert services restarted."
