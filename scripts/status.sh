#!/usr/bin/env bash
set -euo pipefail

SYSTEMCTL="${SYSTEMCTL:-systemctl}"

UNITS=(
  "berial-news.service"
  "berial-ma-alert.service"
  "berial-macd-alert.service"
  "berial-bollinger-alert.service"
  "berial-paper-signal.service"
  "berial-chart-snapshot.service"
  "berial-chart-snapshot.timer"
)

"${SYSTEMCTL}" status --no-pager "${UNITS[@]}"
