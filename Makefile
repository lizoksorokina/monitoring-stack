# ─────────────────────────────────────────────────────────────────────────────
#  Короткие команды для повседневной работы.
#  .RECIPEPREFIX = > заменяет обязательный TAB в рецептах на «>» — так файл
#  не ломается при копировании через Windows/редакторы.
# ─────────────────────────────────────────────────────────────────────────────
.RECIPEPREFIX = >
.DEFAULT_GOAL := help
SHELL := /bin/bash

COMPOSE := docker compose

help: ## Показать список команд
> @grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

up: ## Запустить весь стек (сборка образов)
> $(COMPOSE) up -d --build
> @echo "Grafana:    http://localhost:3000"
> @echo "Prometheus: http://localhost:9090"
> @echo "Alertmanager: http://localhost:9093"

down: ## Остановить и удалить контейнеры (данные в volume остаются)
> $(COMPOSE) down

clean: ## Остановить и удалить ВСЁ вместе с данными (volume)
> $(COMPOSE) down -v

ps: ## Статус контейнеров
> $(COMPOSE) ps

logs: ## Логи всех сервисов (make logs S=prometheus — только одного)
> $(COMPOSE) logs -f --tail=100 $(S)

restart: ## Перезапустить один сервис: make restart S=prometheus
> $(COMPOSE) restart $(S)

reload: ## Перечитать конфиги Prometheus/Alertmanager без перезапуска
> curl -fsS -X POST http://localhost:9090/-/reload && echo "prometheus: reloaded"
> curl -fsS -X POST http://localhost:9093/-/reload && echo "alertmanager: reloaded"

validate: ## Проверить синтаксис конфигов Prometheus (promtool)
> docker run --rm --entrypoint promtool -v "$(PWD)/prometheus:/p:ro" \
> 	prom/prometheus:v3.13.2 check config /p/prometheus.yml
> docker run --rm --entrypoint promtool -v "$(PWD)/prometheus:/p:ro" \
> 	prom/prometheus:v3.13.2 check rules /p/rules/*.yml
> docker run --rm --entrypoint amtool -v "$(PWD)/alertmanager:/a:ro" \
> 	prom/alertmanager:v0.29.0 check-config /a/alertmanager.yml

smoke: ## Прогнать нагрузку по API (чтобы появились графики)
> ./scripts/smoke.sh

chaos-on: ## Включить хаос: 2с задержки + 20% ошибок
> ./scripts/chaos.sh on

chaos-off: ## Выключить хаос
> ./scripts/chaos.sh off

status: ## Что сейчас горит в Alertmanager
> @curl -fsS http://localhost:9093/api/v2/alerts | python3 -m json.tool 2>/dev/null || \
> 	curl -fsS http://localhost:9093/api/v2/alerts
