#!/usr/bin/env python3
"""
alertbot — приёмник вебхуков Alertmanager и мост в Telegram.

Зачем отдельный сервис, если у Alertmanager есть встроенный telegram_configs?
  1. сообщение можно оформить как угодно (HTML, ссылки, группировка);
  2. бот умеет ОТВЕЧАТЬ: /status, /alerts, /help — а не только вещать в чат;
  3. есть DRY_RUN: проект поднимается и всё видно в логах ещё до того,
     как вы завели бота у @BotFather;
  4. бот отдаёт собственные метрики → Prometheus может алертить на то,
     что «алерты перестали доходить». Это и есть «кто сторожит сторожа».

Контракт с Alertmanager: POST /webhook, ответ 2xx = доставлено.
Если ответить 5xx, Alertmanager повторит попытку позже.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import aiohttp
from aiohttp import web
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# ─────────────────────────────────────────────────────────────────────────────
#  Конфигурация из переменных окружения
# ─────────────────────────────────────────────────────────────────────────────


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


LOG_LEVEL = _env("LOG_LEVEL", "INFO").upper()
BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
CHAT_ID = _env("TELEGRAM_CHAT_ID")
THREAD_ID = _env("TELEGRAM_THREAD_ID")
DRY_RUN = _env("TELEGRAM_DRY_RUN", "false").lower() in {"1", "true", "yes", "on"}
TELEGRAM_API = _env("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
PROM_URL = _env("PROMETHEUS_URL", "http://prometheus:9090").rstrip("/")
AM_URL = _env("ALERTMANAGER_URL", "http://alertmanager:9093").rstrip("/")
GRAFANA_URL = _env("GRAFANA_URL", "http://localhost:3000").rstrip("/")

# Ссылки в сообщениях должны открываться в браузере, а внутренние адреса
# Docker (`http://prometheus:9090`) снаружи не резолвятся. Поэтому их два вида.
PROM_PUBLIC_URL = _env("PROMETHEUS_PUBLIC_URL", "http://localhost:9090").rstrip("/")
AM_PUBLIC_URL = _env("ALERTMANAGER_PUBLIC_URL", "http://localhost:9093").rstrip("/")

PORT = int(_env("PORT", "8080"))
ALLOWED_CHATS = {c.strip() for c in _env("TELEGRAM_ALLOWED_CHAT_IDS").split(",") if c.strip()}

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
LOG = logging.getLogger("alertbot")

# ─────────────────────────────────────────────────────────────────────────────
#  Метрики — их собирает Prometheus из job="alertbot"
# ─────────────────────────────────────────────────────────────────────────────

WEBHOOKS = Counter("alertbot_webhooks_total", "Вебхуки от Alertmanager", ["status"])
ALERTS = Counter("alertbot_alerts_total", "Алерты, пришедшие в вебхуках", ["severity", "status"])
SENT_MESSAGES = Counter("alertbot_telegram_sent_total", "Успешно отправленные сообщения")
SEND_ERRORS = Counter("alertbot_telegram_errors_total", "Ошибки отправки в Telegram", ["reason"])
COMMANDS = Counter("alertbot_commands_total", "Обработанные команды", ["command"])
LAST_WEBHOOK = Gauge("alertbot_last_webhook_timestamp_seconds", "Время последнего вебхука (unixtime)")
WEBHOOK_DURATION = Histogram("alertbot_webhook_duration_seconds", "Сколько заняла обработка вебхука")

SEVERITY_ICON = {"critical": "🔥", "warning": "⚠️", "info": "ℹ️"}
STATUS_ICON = {"firing": "🚨", "resolved": "✅"}
MAX_ALERTS_IN_MESSAGE = 8
TELEGRAM_LIMIT = 4000  # реальный лимит 4096, берём с запасом

# ─────────────────────────────────────────────────────────────────────────────
#  Форматирование сообщений
# ─────────────────────────────────────────────────────────────────────────────


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def plain(text: str) -> str:
    """Убрать HTML — для логов и DRY_RUN."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def fmt_time(raw: str | None) -> str:
    if not raw:
        return "—"
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    return moment.astimezone(timezone.utc).strftime("%d.%m %H:%M UTC")


def fmt_float(value: float | None, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "н/д"
    return f"{value:,.{digits}f}".replace(",", " ") + suffix


def render_alert(alert: dict, status: str) -> str:
    labels = alert.get("labels") or {}
    annotations = alert.get("annotations") or {}

    name = labels.get("alertname", "unknown")
    severity = labels.get("severity", "info")
    icon = STATUS_ICON.get(status, "•")
    sev_icon = SEVERITY_ICON.get(severity, "•")

    lines = [f"{icon} <b>{esc(name)}</b> · {sev_icon} {esc(severity)}"]

    summary = annotations.get("summary")
    if summary:
        lines.append(f"<b>{esc(summary)}</b>")

    description = annotations.get("description")
    if description:
        lines.append(esc(description))

    facts = " · ".join(
        f"{key}=<code>{esc(labels[key])}</code>"
        for key in ("instance", "job", "component", "mountpoint", "device", "cluster")
        if labels.get(key)
    )
    if facts:
        lines.append(facts)

    if status == "firing" and alert.get("startsAt"):
        lines.append(f"🕐 горит с {fmt_time(alert['startsAt'])}")
    elif status == "resolved" and alert.get("endsAt"):
        lines.append(f"🕐 восстановился в {fmt_time(alert['endsAt'])}")

    return "\n".join(lines)


def render_webhook(payload: dict) -> str:
    status = payload.get("status", "firing")
    alerts = payload.get("alerts") or []

    if not alerts:
        header = f"{STATUS_ICON.get('resolved', '✅')} <b>Alertmanager: группа пуста</b>"
    elif len(alerts) == 1:
        header = f"<b>Алерт</b>"
    else:
        # Alertmanager присылает пачку: всё, что попало в одну группу
        header = f"<b>Пачка алертов: {len(alerts)}</b>"

    # critical — всегда сверху
    ordered = sorted(
        alerts,
        key=lambda a: (a.get("labels") or {}).get("severity") != "critical",
    )

    blocks = [header]
    for alert in ordered[:MAX_ALERTS_IN_MESSAGE]:
        blocks.append(render_alert(alert, status))
    if len(ordered) > MAX_ALERTS_IN_MESSAGE:
        blocks.append(f"…и ещё {len(ordered) - MAX_ALERTS_IN_MESSAGE}. Полный список — в Alertmanager.")

    footer = (
        f'<a href="{AM_PUBLIC_URL}/#/alerts">Alertmanager</a> · '
        f'<a href="{GRAFANA_URL}/d/app-red">Grafana</a>'
    )

    # Собираем по блокам, чтобы не разрезать HTML-тег на полуслове:
    # обрезанное сообщение Telegram просто отвергнет с "can't parse entities".
    text = ""
    for block in blocks:
        candidate = f"{text}\n\n{block}" if text else block
        if len(candidate) + len(footer) + 2 > TELEGRAM_LIMIT:
            text += "\n\n<i>…сообщение обрезано</i>"
            break
        text = candidate

    return f"{text}\n\n{footer}"


# ─────────────────────────────────────────────────────────────────────────────
#  Отправка в Telegram
# ─────────────────────────────────────────────────────────────────────────────


async def send_telegram(session: aiohttp.ClientSession, text: str, chat_id: str | None = None) -> bool:
    target = chat_id or CHAT_ID

    if DRY_RUN or not BOT_TOKEN or not target:
        reason = "DRY-RUN" if DRY_RUN else "нет токена или chat_id"
        LOG.warning("[%s] сообщение не отправлено:\n%s\n", reason, plain(text))
        return True

    url = f"{TELEGRAM_API}/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": target,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if THREAD_ID:
        try:
            payload["message_thread_id"] = int(THREAD_ID)
        except ValueError:
            LOG.warning("TELEGRAM_THREAD_ID=%r не число — игнорирую", THREAD_ID)

    for attempt in range(1, 4):
        try:
            async with session.post(url, json=payload) as response:
                if response.status == 200:
                    SENT_MESSAGES.inc()
                    return True

                body = await response.text()
                if response.status == 429:
                    retry_after = 5
                    try:
                        retry_after = int(json.loads(body)["parameters"]["retry_after"])
                    except (KeyError, TypeError, ValueError):
                        pass
                    LOG.warning("Telegram: rate limit, ждём %s с", retry_after)
                    await asyncio.sleep(min(retry_after, 30))
                    continue

                SEND_ERRORS.labels(reason=str(response.status)).inc()
                LOG.error("Telegram ответил %s: %s", response.status, body[:300])
                if 400 <= response.status < 500:
                    # Кривой токен / chat_id / разметка — ретраить бессмысленно
                    return False
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            SEND_ERRORS.labels(reason="network").inc()
            LOG.error("Сеть при отправке в Telegram: %s", exc)

        await asyncio.sleep(2 ** attempt)

    return False


# ─────────────────────────────────────────────────────────────────────────────
#  HTTP-ручки
# ─────────────────────────────────────────────────────────────────────────────


async def handle_webhook(request: web.Request) -> web.Response:
    started = time.perf_counter()
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        WEBHOOKS.labels(status="bad_request").inc()
        return web.json_response({"error": "invalid json"}, status=400)

    status = payload.get("status", "unknown")
    alerts = payload.get("alerts") or []

    WEBHOOKS.labels(status=status).inc()
    LAST_WEBHOOK.set(time.time())
    for alert in alerts:
        severity = (alert.get("labels") or {}).get("severity", "none")
        ALERTS.labels(severity=severity, status=status).inc()

    delivered = await send_telegram(request.app["session"], render_webhook(payload))
    WEBHOOK_DURATION.observe(time.perf_counter() - started)

    LOG.info(
        "Вебхук: status=%s, алертов=%s, доставлено=%s",
        status,
        len(alerts),
        delivered,
    )
    # 5xx заставит Alertmanager повторить — это лучше, чем потерять алерт
    return web.json_response(
        {"delivered": delivered, "alerts": len(alerts)},
        status=200 if delivered else 502,
    )


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "status": "ok",
            "telegram_configured": bool(BOT_TOKEN and CHAT_ID),
            "dry_run": DRY_RUN,
        }
    )


async def handle_metrics(request: web.Request) -> web.Response:
    return web.Response(body=generate_latest(), headers={"Content-Type": CONTENT_TYPE_LATEST})


async def handle_index(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "service": "alertbot",
            "endpoints": ["POST /webhook", "GET /health", "GET /metrics"],
            "dry_run": DRY_RUN,
            "telegram_configured": bool(BOT_TOKEN and CHAT_ID),
        }
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Команды бота
# ─────────────────────────────────────────────────────────────────────────────

HELP_TEXT = (
    "🤖 <b>alertbot</b> — доставка алертов Alertmanager в Telegram.\n\n"
    "<b>Команды</b>\n"
    "/status — короткая сводка по стеку\n"
    "/alerts — что сейчас горит\n"
    "/help — это сообщение\n\n"
    f'<a href="{GRAFANA_URL}">Grafana</a> · <a href="{PROM_PUBLIC_URL}">Prometheus</a> · '
    f'<a href="{AM_PUBLIC_URL}">Alertmanager</a>'
)


async def prom_scalar(session: aiohttp.ClientSession, expression: str) -> float | None:
    """Выполнить instant-запрос и вернуть первое число."""
    try:
        async with session.get(f"{PROM_URL}/api/v1/query", params={"query": expression}) as response:
            if response.status != 200:
                return None
            data = await response.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError):
        return None

    result = (data.get("data") or {}).get("result") or []
    if not result:
        return 0.0
    try:
        return float(result[0]["value"][1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


async def active_alerts(session: aiohttp.ClientSession) -> list[dict]:
    try:
        async with session.get(
            f"{AM_URL}/api/v2/alerts", params={"active": "true", "silenced": "false"}
        ) as response:
            if response.status != 200:
                return []
            return await response.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError):
        return []


async def cmd_status(session: aiohttp.ClientSession) -> str:
    firing = await prom_scalar(session, 'sum(ALERTS{alertstate="firing"})')
    down = await prom_scalar(session, "count(up == 0)")
    rps = await prom_scalar(session, 'sum(job:http_requests:rate5m{job="app"})')
    errors = await prom_scalar(session, 'job:http_errors:ratio5m{job="app"}')
    p95 = await prom_scalar(session, 'job:http_request_duration_seconds:p95{job="app"}')

    error_text = "н/д" if errors is None else f"{errors * 100:.2f}%"

    return "\n".join(
        [
            "📊 <b>Статус стека</b>",
            f"• Горящих алертов: <b>{fmt_float(firing, 0)}</b>",
            f"• Целей недоступно: <b>{fmt_float(down, 0)}</b>",
            f"• Запросов в секунду: <b>{fmt_float(rps, 1)}</b>",
            f"• Доля ошибок 5xx: <b>{error_text}</b>",
            f"• p95 времени ответа: <b>{fmt_float(p95, 3, ' с')}</b>",
        ]
    )


async def cmd_alerts(session: aiohttp.ClientSession) -> str:
    alerts = await active_alerts(session)
    if not alerts:
        return "✅ Активных алертов нет."

    lines = [f"🚨 <b>Активных алертов: {len(alerts)}</b>", ""]
    for alert in alerts[:10]:
        labels = alert.get("labels") or {}
        severity = labels.get("severity", "info")
        lines.append(
            f"{SEVERITY_ICON.get(severity, '•')} <b>{esc(labels.get('alertname', '?'))}</b> — "
            f"{esc(labels.get('instance', '—'))}"
        )
    if len(alerts) > 10:
        lines.append(f"…и ещё {len(alerts) - 10}")
    return "\n".join(lines)


async def handle_command(app: web.Application, command: str, chat_id: str) -> None:
    COMMANDS.labels(command=command).inc()
    session = app["session"]

    if command in ("/start", "/help"):
        await send_telegram(session, HELP_TEXT, chat_id)
    elif command == "/status":
        await send_telegram(session, await cmd_status(session), chat_id)
    elif command == "/alerts":
        await send_telegram(session, await cmd_alerts(session), chat_id)
    else:
        await send_telegram(
            session,
            f"Неизвестная команда <code>{esc(command)}</code>. Попробуйте /help",
            chat_id,
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Long polling: получаем команды без вебхука и публичного домена
# ─────────────────────────────────────────────────────────────────────────────


async def api_loop(app: web.Application) -> None:
    session: aiohttp.ClientSession = app["session"]
    offset = 0
    LOG.info("Опрос команд Telegram запущен")

    while True:
        try:
            async with session.get(
                f"{TELEGRAM_API}/bot{BOT_TOKEN}/getUpdates",
                params={"offset": offset, "timeout": 25},
                timeout=aiohttp.ClientTimeout(total=40),
            ) as response:
                if response.status != 200:
                    LOG.warning("getUpdates: HTTP %s", response.status)
                    await asyncio.sleep(5)
                    continue
                data = await response.json()

            for update in data.get("result") or []:
                offset = update.get("update_id", offset) + 1
                message = update.get("message") or update.get("channel_post") or {}
                text = (message.get("text") or "").strip()
                chat_id = str((message.get("chat") or {}).get("id", ""))

                if not text.startswith("/"):
                    continue
                if ALLOWED_CHATS and chat_id not in ALLOWED_CHATS:
                    LOG.warning("Команда из чужого чата %s проигнорирована", chat_id)
                    continue

                command = text.split()[0].split("@")[0].lower()
                await handle_command(app, command, chat_id)

        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — цикл не должен умирать
            LOG.warning("Ошибка опроса Telegram: %s", exc)
            await asyncio.sleep(5)


# ─────────────────────────────────────────────────────────────────────────────
#  Жизненный цикл приложения
# ─────────────────────────────────────────────────────────────────────────────


async def on_startup(app: web.Application) -> None:
    app["session"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))

    if DRY_RUN:
        LOG.warning("DRY_RUN=true — сообщения только в лог, Telegram не трогаем")
    elif not (BOT_TOKEN and CHAT_ID):
        LOG.warning("TELEGRAM_BOT_TOKEN/CHAT_ID не заданы — работаю как лог-приёмник")
    else:
        await send_telegram(app["session"], "🟢 <b>alertbot</b> запущен и слушает Alertmanager.")

    if BOT_TOKEN and not DRY_RUN:
        app["poller"] = asyncio.create_task(api_loop(app))


async def on_cleanup(app: web.Application) -> None:
    poller: asyncio.Task | None = app.get("poller")
    if poller:
        poller.cancel()
        try:
            await poller
        except asyncio.CancelledError:
            pass
    await app["session"].close()


def create_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/", handle_index)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/metrics", handle_metrics)
    app.router.add_post("/webhook", handle_webhook)
    return app


if __name__ == "__main__":
    LOG.info("alertbot стартует на 0.0.0.0:%s (dry_run=%s)", PORT, DRY_RUN)
    web.run_app(create_app(), host="0.0.0.0", port=PORT, print=None)
