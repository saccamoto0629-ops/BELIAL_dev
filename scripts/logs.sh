#!/usr/bin/env bash
set -euo pipefail

JOURNALCTL="${JOURNALCTL:-journalctl}"

usage() {
  cat <<'USAGE'
Usage: ./scripts/logs.sh <target> [journalctl args...]

Targets:
  news        berial-news.service
  ma          berial-ma-alert.service
  macd        berial-macd-alert.service
  bollinger   berial-bollinger-alert.service
  snapshot    berial-chart-snapshot.service
  timer       berial-chart-snapshot.timer
  all         all Berial units

Examples:
  ./scripts/logs.sh ma
  ./scripts/logs.sh snapshot -n 100
  ./scripts/logs.sh all --since today
USAGE
}

if [[ $# -lt 1 ]]; then
  usage >&2
  exit 2
fi

target="$1"
shift

case "${target}" in
  news)
    units=("berial-news.service")
    ;;
  ma)
    units=("berial-ma-alert.service")
    ;;
  macd)
    units=("berial-macd-alert.service")
    ;;
  bollinger)
    units=("berial-bollinger-alert.service")
    ;;
  snapshot)
    units=("berial-chart-snapshot.service")
    ;;
  timer)
    units=("berial-chart-snapshot.timer")
    ;;
  all)
    units=(
      "berial-news.service"
      "berial-ma-alert.service"
      "berial-macd-alert.service"
      "berial-bollinger-alert.service"
      "berial-chart-snapshot.service"
      "berial-chart-snapshot.timer"
    )
    ;;
  -h|--help|help)
    usage
    exit 0
    ;;
  *)
    echo "Unknown log target: ${target}" >&2
    usage >&2
    exit 2
    ;;
esac

args=()
if [[ $# -eq 0 ]]; then
  args=(-f -n 200)
else
  args=("$@")
fi

journal_args=()
for unit in "${units[@]}"; do
  journal_args+=(-u "${unit}")
done

"${JOURNALCTL}" "${journal_args[@]}" "${args[@]}"
