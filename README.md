# Monitoring Stack

![Prometheus](https://img.shields.io/badge/Prometheus-3.13-E6522C?logo=prometheus&logoColor=white)
![Grafana](https://img.shields.io/badge/Grafana-12.1-F46800?logo=grafana&logoColor=white)
![Alertmanager](https://img.shields.io/badge/Alertmanager-0.29-E6522C?logo=prometheus&logoColor=white)
![Docker](https://img.shields.io/badge/Docker_Compose-v2-2496ED?logo=docker&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)

Стенд мониторинга и алертинга  в Docker: Prometheus собирает метрики,
Grafana отрисовывает дашборды, Alertmanager сортирует алерты, Telegram-бот присылает их в чат.

Запускается одной командой в WSL.

```
сервис → /metrics → Prometheus → правило → Alertmanager → бот → Telegram
                                        └→ Grafana (визуализация)
```


## Что мониторится

Есть небольшой сервис заказов (`app`) на FastAPI с базой PostgreSQL и прокси nginx —
на нём удобно показывать и здоровые метрики, и деградацию. У сервиса есть эндпоинт
`/api/chaos`, который искусственно замедляет ответы и добавляет ошибки 5xx,
чтобы алерты можно было увидеть вживую, а не поверить на слово.

| Слой | Источник метрик |
|---|---|
| Приложение | `app` — HTTP-метрики, гистограмма задержек, заказы, ошибки БД |
| Хост (WSL) | `node-exporter` — CPU, память, диски, сеть, load average |
| Веб-сервер | `nginx-exporter` — соединения и запросы nginx |
| База данных | `postgres-exporter` — состояние БД и соединения |
| Синтетика | `blackbox-exporter` — доступность сервисов «снаружи» |
| Сам мониторинг | Prometheus, Alertmanager, Grafana, alertbot |

---

## Схема

```
                        ┌──── nginx ──── app (FastAPI) ──── postgres
                        │       │            │                │
                        │  nginx-exp      /metrics       postgres-exp
                        │       │            │                │
   node-exporter ───────┼───────┴────────────┴────────────────┘
   (хост WSL)           │
   blackbox-exporter ───┤
   (пробы снаружи)      ▼
                  ┌──────────────┐        ┌───────────────┐
                  │  PROMETHEUS  │───────▶│ ALERTMANAGER  │
                  │  правила     │ alerts │ группировка   │
                  └──────┬───────┘        │ инхибиторы    │
                         │                └───────┬───────┘
                  ┌──────▼───────┐                │ webhook
                  │   GRAFANA    │        ┌───────▼───────┐
                  │  3 дашборда  │        │   alertbot    │
                  └──────────────┘        └───────┬───────┘
                                                  ▼
                                              TELEGRAM
```

---

## Быстрый старт

```bash
git clone git@github.com:lizoksorokina/monitoring-stack.git
cd monitoring-stack

cp .env.example .env
nano .env                      # вписать TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID

docker compose up -d --build
docker compose ps
```

Открыть:

| Сервис | Адрес | Что там |
|---|---|---|
| Grafana | http://localhost:3000 | дашборды, вход `admin` / `admin` |
| Prometheus | http://localhost:9090 | метрики, цели, правила |
| Alertmanager | http://localhost:9093 | активные алерты, silences |
| Приложение | http://localhost:8080/docs | Swagger демо-сервиса |



### Telegram

1. В Telegram: **@BotFather** → `/newbot` → получить токен.
2. Написать своему боту любое сообщение (бот не может писать первым).
3. Узнать `chat_id`: **@userinfobot** для лички или **@RawDataBot** для группы (там он вида `-100...`).
4. Вписать в `.env` и перезапустить бота:

```bash
docker compose up -d alertbot
```

Без токена стенд тоже работает — бот в этом случае печатает сообщения в лог
(`docker compose logs -f alertbot`)

---

## Куда смотреть

### Prometheus — http://localhost:9090

- **Status → Targets** — 11 целей, все должны быть `UP`;
- **Status → Rules** — 21 alerting-правило и 14 recording-правил;
- **Graph** — любой PromQL руками, например `rate(http_requests_total{job="app"}[5m])`;
- **Alerts** — что горит и что в состоянии `pending`.

### Grafana — http://localhost:3000

Три дашборда, все создаются автоматически при старте:

1. **API — RED и SLO.** Трафик, доля ошибок, перцентили задержки, доступность
   за час против SLO 99.5%, заказы и отказы платежей.
2. **Инфраструктура — USE.** CPU, память, диски, сеть и load average хоста WSL.
3. **Платформа.** Что горит прямо сейчас, состояние всех целей, история алертов
   и доставка уведомлений в Telegram.

### Alertmanager — http://localhost:9093

- **Alerts** — активные алерты и их состояние (`active`, `suppressed`);
- **Silences** — заглушки, например на время плановых работ;
- у подавленного алерта видно причину — сработал инхибитор.

### Telegram-бот

Присылает алерты с уровнем важности, инстансом и описанием; отдельным сообщением
сообщает о восстановлении. Отвечает на команды `/status` и `/alerts`.

---

## Алерты

Правила лежат в `prometheus/rules/` и разбиты по смыслу.

| Алерт | Условие | Severity |
|---|---|---|
| `ServiceDown` | сервис не отдаёт метрики дольше минуты | critical |
| `TargetDown` | любая цель не скрейпится 5 минут | warning |
| `HighErrorRate` | доля 5xx выше 5% пять минут | warning |
| `SLOSlowBurn` | сгорает 6 бюджетов ошибок за 6 часов | warning |
| `SLOFastBurn` | сгорает 14.4 бюджета за 1 час | critical |
| `HighLatencyP95` | p95 задержки выше 1.5 с десять минут | warning |
| `HostHighCpuUsage` / `HostHighMemoryUsage` | загрузка выше 85% / 90% | warning |
| `HostDiskLow` / `HostDiskCritical` | диск занят больше 85% / 95% | warning / critical |
| `PostgresDown` / `NginxDown` | база или nginx недоступны | critical |
| `ProbeFailed` | внешняя проба не проходит 3 минуты | critical |
| `AlertmanagerDown` / `AlertbotDown` | сломана доставка алертов | critical |

SLO-алерты считают не «сколько ошибок», а **скорость прожига бюджета**: при SLO 99.5%
бюджет ошибок — 0.5%, и если он тратится в 14.4 раза быстрее нормы, это уже пожар.

---

## Проверка алертов

Запустить деградацию сервиса и посмотреть, что произойдёт:

```bash
./scripts/chaos.sh on          # 2 с задержки и 20% ошибок 5xx
REQUESTS=120 ./scripts/smoke.sh
```

Дальше по времени:

| Через | Что появится |
|---|---|
| ~10 мин | `HighLatencyP95` и `HighErrorRate` (pending → firing) |
| ~22 мин | `SLOFastBurn` — за час накопилось больше 7.2% ошибок |

Выключить и дождаться сообщений о восстановлении:

```bash
./scripts/chaos.sh off
```

Ещё один показательный опыт — инхибиторы. Уроните приложение:

```bash
docker compose stop app
```

Загорится `ServiceDown`, а `HighErrorRate` и `HighLatencyP95` станут `suppressed`:
Alertmanager не шлёт следствия, когда известна причина. Поднять обратно —
`docker compose start app`.

---

## Структура

```
monitoring-stack/
├── docker-compose.yml          # весь стенд: 11 сервисов
├── .env.example                # шаблон секретов
├── Makefile                    # короткие команды
├── app/                        # демо-сервис на FastAPI
├── alertbot/                   # приёмник вебхуков Alertmanager → Telegram
├── nginx/default.conf          # прокси и stub_status
├── blackbox/blackbox.yml       # модули синтетических проб
├── prometheus/
│   ├── prometheus.yml          # цели и куда слать алерты
│   └── rules/                  # recording и alerting правила
├── alertmanager/               # маршруты, инхибиторы, повторы
├── grafana/
│   ├── provisioning/           # datasource и провайдер дашбордов
│   └── dashboards/             # 3 дашборда в JSON
└── scripts/                    # smoke.sh, chaos.sh
```

---

## Команды

```bash
make up          # поднять стенд
make down        # остановить
make logs        # логи всех, или make logs S=prometheus
make validate    # promtool check config + amtool check-config
make reload      # перечитать конфиги без перезапуска
make status      # что сейчас горит
make chaos-on    # включить деградацию
make chaos-off   # выключить
```

---

## Что можно добавить

- логи — Loki с Promtail;
- трейсы — Tempo и OpenTelemetry в приложении;
- нагрузочное тестирование — k6 вместо bash-цикла;
- CI — `make validate` в GitHub Actions на каждый push;
- долгое хранение метрик — remote write в VictoriaMetrics или Mimir.
