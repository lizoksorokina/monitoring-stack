#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  Генератор нагрузки: без трафика графики пустые, а алерты бессмысленны.
#  Идёт через nginx (порт 8080), то есть проверяет весь путь запроса.
#
#  Использование:
#    ./scripts/smoke.sh                 # 300 итераций
#    REQUESTS=2000 ./scripts/smoke.sh   # подольше
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8080}"
REQUESTS="${REQUESTS:-300}"
ITEMS=(coffee_beans green_tea notebook headphones usb_cable desk_lamp)

if ! curl -fsS -o /dev/null --max-time 5 "${BASE_URL}/nginx-health"; then
  echo "❌ ${BASE_URL} недоступен. Стек запущен? (make up)"
  exit 1
fi

echo "→ Нагрузка на ${BASE_URL}: ${REQUESTS} итераций по 2 запроса"

for ((i = 1; i <= REQUESTS; i++)); do
  item="${ITEMS[RANDOM % ${#ITEMS[@]}]}"
  quantity=$((RANDOM % 3 + 1))
  price=$((RANDOM % 90 + 10))

  curl -s -o /dev/null --max-time 10 -X POST "${BASE_URL}/api/orders" \
    -H 'Content-Type: application/json' \
    -d "{\"item\":\"${item}\",\"quantity\":${quantity},\"price\":${price}.50}" || true

  curl -s -o /dev/null --max-time 10 "${BASE_URL}/api/orders?limit=10" || true

  if (( i % 50 == 0 )); then
    echo "   … ${i}/${REQUESTS}"
  fi
done

echo "✅ Готово. Grafana: http://localhost:3000 (дашборд «1 · API — RED и SLO»)"
