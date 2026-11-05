#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  Управляемая деградация сервиса — чтобы алерты сработали по-настоящему.
#
#    ./scripts/chaos.sh on       # 2 с задержки + 20% ошибок 5xx
#    ./scripts/chaos.sh status
#    ./scripts/chaos.sh off
#
#  Свои значения:
#    LATENCY_MS=2000 ERROR_RATE=0.5 ./scripts/chaos.sh on
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8080}"
LATENCY_MS="${LATENCY_MS:-2000}"
ERROR_RATE="${ERROR_RATE:-0.2}"

usage() {
  echo "Использование: $0 {on|off|status}"
  exit 1
}

case "${1:-}" in
  on)
    echo "🔥 Включаю хаос: задержка ${LATENCY_MS} мс, доля ошибок ${ERROR_RATE}"
    curl -fsS -X POST "${BASE_URL}/api/chaos?latency_ms=${LATENCY_MS}&error_rate=${ERROR_RATE}"
    echo
    echo "Теперь запустите ./scripts/smoke.sh — через 5-10 минут загорятся HighLatencyP95 и HighErrorRate."
    ;;
  off)
    echo "💚 Выключаю хаос"
    curl -fsS -X DELETE "${BASE_URL}/api/chaos"
    echo
    ;;
  status)
    curl -fsS "${BASE_URL}/api/chaos"
    echo
    ;;
  *)
    usage
    ;;
esac
