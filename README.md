# Pet-проект: Prometheus + Grafana + Alertmanager + Telegram-бот

Полный цикл наблюдаемости на одном ноутбуке: сервис, за которым наблюдаем, экспортёры,
сбор метрик, правила, алерты, маршрутизация и доставка в Telegram — плюс дашборды
и метрики самого мониторинга.

Всё поднимается одной командой в WSL: `docker compose up -d --build`.

---

## Схема

```
                    ┌──────────────────────────────────────────────┐
                    │             МОНИТОРИРУЕМОЕ                   │
                    │                                              │
   клиент ──8080──► nginx ──► app (FastAPI) ──► postgres            │
                    │  │          │                  │             │
                    │  │          └─ /metrics        └─ postgres-   │
                    │  │             │                   exporter   │
                    │  └─ /stub_status │                     │      │
                    │        │         │                     │      │
                    │  nginx-exporter  │                     │      │
                    └────────┼─────────┼─────────────────────┼──────┘
                             │         │                     │
   node-exporter ────────────┼─────────┼─────────────────────┤
   (CPU/RAM/диск хоста WSL)  │         │                     │
                             ▼         ▼                     ▼
                    ┌──────────────────────────────────────────────┐
                    │                PROMETHEUS                    │
                    │  scrape 15s · recording rules · alert rules  │
                    └───────┬───────────────────────┬──────────────┘
                            │                       │
                 blackbox-exporter                │ alerts
                 (пробы снаружи:                 ▼
                  probe_success)        ┌──────────────────┐
                            ▲           │   ALERTMANAGER   │
                            └───────────┤ группировка      │
                                        │ инхибиторы       │
   grafana / alertmanager / alertbot ──►│ повторы          │
   (мониторим сам мониторинг)           └────────┬─────────┘
                                                 │ webhook
                                                 ▼
                                        ┌──────────────────┐
                                        │     alertbot     │
                                        │  форматирование  │
                                        │  команды /status │
                                        └────────┬─────────┘
                                                 │ Bot API
                                                 ▼
                                             TELEGRAM
```

## Сервисы и порты

| Сервис              | Порт  | Роль                                                        |
| ------------------- | ----- | ----------------------------------------------------------- |
| Grafana             | 3000  | Дашборды (провижинятся из файлов)                            |
| Prometheus          | 9090  | Сбор и хранение метрик, вычисление правил                    |
| Alertmanager        | 9093  | Группировка, подавление и повторы алертов                    |
| alertbot            | 8081  | Приёмник вебхуков AM + бот Telegram (`/status`, `/alerts`)   |
| nginx               | 8080  | Точка входа в приложение, `stub_status`                      |
| app                 | 8000  | Демо-сервис заказов с метриками и `/api/chaos`               |
| node-exporter       | 9100  | CPU / RAM / диски / сеть хоста WSL                           |
| nginx-exporter      | 9113  | Метрики nginx                                                |
| postgres-exporter   | 9187  | Метрики базы                                                 |
| blackbox-exporter   | 9115  | Синтетические HTTP-пробы «снаружи»                           |
| postgres            | —     | База демо-сервиса                                            |

---

## Быстрый старт (WSL)

### 0. Что нужно

```bash
docker --version          # Docker 24+ (Docker Desktop с интеграцией в WSL — ок)
docker compose version    # Compose v2
```

### 1. Положить проект в /opt/grafana

```bash
# старую версию — в сторону, чтобы не смешивать файлы
sudo mv /opt/grafana /opt/grafana.old.$(date +%F)
sudo mkdir -p /opt/grafana
sudo chown -R "$USER":"$USER" /opt/grafana

# копируем из Windows-папки (путь поправьте под себя)
cp -r /mnt/d/Prometheus+Grafana/monitoring-stack/. /opt/grafana/
cd /opt/grafana
```

> Если старый стек ещё запущен — сначала `docker compose down` в старой папке,
> иначе порты 3000/9090/9093/9100 будут заняты.

### 2. Настроить окружение

```bash
cp .env.example .env
nano .env          # как минимум — токен и chat_id (см. следующий раздел)
```

### 3. Запустить

```bash
docker compose up -d --build
docker compose ps          # все сервисы должны стать healthy/running
```

### 4. Открыть

- Grafana — <http://localhost:3000> (логин/пароль из `.env`, по умолчанию `admin`/`admin`)
- Prometheus — <http://localhost:9090>
- Alertmanager — <http://localhost:9093>
- Приложение — <http://localhost:8080/docs> (Swagger)

Порты WSL2 пробрасываются в Windows автоматически: открывайте эти адреса прямо в браузере Windows.

### 5. Дать данные

```bash
chmod +x scripts/*.sh
./scripts/smoke.sh
```

Через минуту на дашборде «1 · API — RED и SLO» появятся графики.

---

## Telegram-бот за пять минут

1. В Telegram открыть **@BotFather** → `/newbot` → придумать имя → получить токен
   вида `123456789:AAH...`.
2. Написать своему боту любое сообщение (иначе он не сможет первым вам писать).
3. Узнать свой `chat_id`: написать **@userinfobot** — он покажет `Id`.
   (Для группы: добавить бота в группу и взять id через **@RawDataBot**, он вида `-100...`.)
4. Вписать в `.env`:

   ```env
   TELEGRAM_BOT_TOKEN=123456789:AAH...
   TELEGRAM_CHAT_ID=123456789
   ```

5. Перезапустить бота:

   ```bash
   docker compose up -d alertbot
   docker compose logs -f alertbot
   ```

   В чат должно прийти «🟢 alertbot запущен».

6. Проверить бота руками — напишите ему `/status`. Он ответит сводкой по стеку.

**Хотите сначала посмотреть без Telegram?** Оставьте `TELEGRAM_DRY_RUN=true`:
бот будет печатать готовые сообщения в `docker compose logs -f alertbot`
и ничего не отправит.

---

## Проверка, что алерты реально работают

```bash
./scripts/chaos.sh on        # приложение начнёт отвечать ~2 с и падать в 5xx в 20% случаев
REQUESTS=120 ./scripts/smoke.sh
```

Что произойдёт (по времени от старта нагрузки):

| Через  | Что                                                                    |
| ------ | ---------------------------------------------------------------------- |
| ~5 мин | `HostHighCpuUsage`, возможно `HighLatencyP95` (pending → firing)        |
| ~10 мин| **HighLatencyP95** — 800 мс…2 с на p95 держатся 10 минут                |
| ~10 мин| **HighErrorRate** — доля 5xx больше 5% пять минут                       |
| ~22 мин| **SLOFastBurn** — за час накопилось больше 7.2% ошибок                  |
| ~30 мин| `SLOSlowBurn`, если не выключить хаос                                   |

Выключить и посмотреть на `resolved`-сообщения:

```bash
./scripts/chaos.sh off
```

Почему `SLOFastBurn` такой «медленный»: окно 1 час — это осознанно, именно так
устроен multi-window burn-rate из SRE Workbook. Если хочется быстрее для демо,
поменяйте в `prometheus/rules/alerts-api.yml` окно на `job:http_errors:ratio5m`
и порог на `0.072` — но тогда это уже не «часовой» алерт.

### Что посмотреть в Alertmanager

Откройте <http://localhost:9093>:

- **Alerts** — все алерты и их состояние (`active`, `suppressed`, `unprocessed`);
- **Silences** — заглушки (полезно: «плановые работы» → silence на 1 час);
- у `suppressed` видно причину — сработал инхибитор.

Проверить инхибиторы руками: уроните приложение
(`docker compose stop app`). Загорится `ServiceDown` (critical), а `HighErrorRate`
и `HighLatencyP95` окажутся `suppressed` — это и есть подавление симптомов
причиной. Через минуту поднимите: `docker compose start app`.

---

## Структура

```
monitoring-stack/
├── docker-compose.yml          # весь стенд
├── .env.example                # шаблон секретов
├── Makefile                    # up / down / logs / validate / smoke / chaos
├── app/                        # демо-сервис: FastAPI + prometheus-client
├── alertbot/                   # приёмник вебхуков AM → Telegram
├── nginx/default.conf          # прокси + stub_status
├── blackbox/blackbox.yml       # модули проб
├── prometheus/
│   ├── prometheus.yml          # что скрейпить и куда слать алерты
│   └── rules/
│       ├── recording.yml       # заранее посчитанные выражения
│       ├── alerts-api.yml      # алерты приложения + SLO
│       ├── alerts-infra.yml    # хост, nginx, postgres
│       └── alerts-platform.yml # сам мониторинг и синтетика
├── alertmanager/alertmanager.yml
├── grafana/
│   ├── provisioning/           # datasource + провайдер дашбордов
│   └── dashboards/*.json       # 3 дашборда, эталон в git
└── scripts/                    # smoke.sh, chaos.sh
```

---

## Ключевые решения (и почему именно так)

Это то, о чём спрашивают на собеседовании, поэтому проговорено явно.

**Гистограмма, а не Summary.** `http_request_duration_seconds` — гистограмма,
поэтому перцентили можно складывать по всем инстансам. Квантили из summary
агрегировать нельзя — это классическая ловушка.

**Recording rules, а не «формула в каждом месте».** Тяжёлые выражения считаются
один раз и сохраняются как `job:http_errors:ratio5m`. Алерт и график читают одну
и ту же метрику — значит, они не могут «показывать разное».

**Алерт не на симптом, а на бюджет ошибок.** Кроме «доля 5xx > 5%» есть
burn-rate алерты под SLO 99.5%: быстрый прожиг (14.4× за 1 ч) и медленный (6× за 6 ч).
Именно так отличают «всё сломалось» от «потихоньку деградируем».

**`up` и `probe_success` — разные вещи.** `up` говорит «Prometheus смог
скрейпить», `probe_success` — «реальный клиент смог получить ответ». Может быть
`up = 1`, а `probe_success = 0`: сервис отдаёт метрики, но не обслуживает трафик.

**Алерты с `for`.** Почти все условия должны продержаться 5–15 минут. Без этого
любой всплеск становится инцидентом и алертам перестают верить — это причина №1,
почему мониторинг «выключают».

**Инхибиторы, а не «просто больше алертов».** Если сервис лежит, нет смысла
отдельно сообщать про его латентность. `ServiceDown` подавляет `HighErrorRate`,
`HighLatencyP95` и `TargetDown` по тому же `instance`.

**Свой бот вместо встроенного `telegram_configs`.** Встроенный умеет только
вещать. Свой — умеет `/status` и `/alerts`, отдаёт собственные метрики
(`alertbot_telegram_errors_total`), поэтому на «алерты не доходят» тоже можно
завести алерт. И у него есть `DRY_RUN`.

**Мониторим мониторинг.** Prometheus скрейпит сам себя, Alertmanager, бота и
Grafana. Если упадёт доставка алертов, об этом сообщит алерт — пусть и в тот же
сломанный канал; зато это видно в Alertmanager UI и на дашборде «3 · Платформа».

---

## Как расширять

### Добавить новую цель для сбора метрик

1. Поднять экспортёр в `docker-compose.yml` (у него должен быть `/metrics`).
2. Добавить job в `prometheus/prometheus.yml`:

   ```yaml
   - job_name: my-service
     static_configs:
       - targets: ["my-service:9000"]
         labels:
           component: backend
   ```

3. `make validate && make reload`
4. Проверить: <http://localhost:9090/targets> → цель должна быть `UP`.

### Добавить алерт

```yaml
- alert: MyName
  expr: <promql, результат 0/1 или число>
  for: 5m
  labels:
    severity: warning        # critical | warning
    component: my-service
  annotations:
    summary: "Коротко, что случилось: {{ $labels.instance }}"
    description: "Подробнее: значение {{ $value | humanizePercentage }}"
```

`summary` и `description` уходят в Telegram как есть — пишите по-человечески.
`severity: critical` → отдельный маршрут (без задержки, повтор каждый час).

### Добавить дашборд

Положить JSON в `grafana/dashboards/` и добавить тег `pet`, если хотите видеть
его в выпадающем списке «Все дашборды». Перезапуск не нужен — Grafana
перечитывает папку каждые 30 секунд.

---

## Диагностика

| Симптом | Что делать |
| --- | --- |
| `port is already allocated` | Старый стек ещё жив: `docker ps`, затем `docker compose down` в старой папке |
| Дашборд не появился | `docker compose logs grafana \| grep -i dash` — Grafana пишет, какой JSON не распарсился |
| Prometheus ругается на конфиг | `make validate` покажет правило и строку; логи: `docker compose logs prometheus` |
| Цель `UP` = 0 | <http://localhost:9090/targets> → сообщение об ошибке скрейпа (обычно неверный порт или `metrics_path`) |
| Алерт горит, в Telegram тишина | `docker compose logs alertbot` → затем `curl -X POST http://localhost:9093/-/reload` |
| Бот молчит, ошибок нет | Проверьте `TELEGRAM_DRY_RUN=false`, `TELEGRAM_CHAT_ID` (для группы — с минусом) и что вы написали боту первым |
| Диск в дашборде пустой | node-exporter собирает только `ext4/xfs/btrfs`; WSL-разделы `/mnt/c` (drvfs) намеренно исключены |
| Всё «по нулям» | Нет трафика: `./scripts/smoke.sh` |

Полезные команды:

```bash
make ps
make logs S=prometheus
make validate       # promtool check config + check rules + amtool check-config
make reload         # перечитать конфиги без перезапуска
make status         # активные алерты из Alertmanager API
```

---

## Что можно добавить дальше

- **Логи** — Loki + Promtail, связать с метриками через `exemplars`;
- **Трейсы** — Tempo + OpenTelemetry в приложении;
- **Нагрузочное тестирование** — k6 вместо bash-цикла, метрики в Prometheus;
- **Grafana Alerting** — перенести правила в Grafana и сравнить подходы;
- **Долгое хранение** — `remote_write` в VictoriaMetrics/Thanos/Mimir;
- **CI** — `make validate` в GitLab CI на каждый push (promtool в контейнере уже готов);
- **Несколько инстансов** — поднять второй app и посмотреть, как считаются
  перцентили и алерты при `sum by (instance)`.
