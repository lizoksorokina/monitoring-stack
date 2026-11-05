#!/usr/bin/env python3
"""
order-service — небольшой, но «настоящий» сервис, который мы мониторим.

Почему не счётчик из учебника: алерты должно быть на чём проверять.
Поэтому здесь есть:
  • HTTP-метрики в стиле RED (rate / errors / duration);
  • бизнес-метрики (заказы, отклонённые платежи);
  • метрики обращений к БД;
  • /api/chaos — управляемая деградация, чтобы алерты реально загорались.

Важное свойство: /metrics и /health работают, даже если БД недоступна.
Иначе при падении базы Prometheus получил бы up=0 и мы потеряли бы
самые нужные в этот момент метрики.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import asyncpg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel, Field

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
LOG = logging.getLogger("order-service")

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SERVICE_NAME = os.getenv("SERVICE_NAME", "order-service")
PAYMENT_FAILURE_RATE = float(os.getenv("PAYMENT_FAILURE_RATE", "0.03"))

# ─────────────────────────────────────────────────────────────────────────────
#  Метрики
# ─────────────────────────────────────────────────────────────────────────────

REQUEST_TOTAL = Counter(
    "http_requests_total",
    "Количество HTTP-запросов",
    ["method", "path", "status"],
)

# Гистограмма, а не Summary: перцентили нужно уметь агрегировать по всем
# инстансам сразу, а summary-квантили складывать нельзя.
REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "Длительность обработки HTTP-запроса",
    ["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

INFLIGHT = Gauge("http_inflight_requests", "Запросы, которые сейчас обрабатываются")

ORDERS_CREATED = Counter("app_orders_created_total", "Создано заказов")
PAYMENTS_FAILED = Counter("app_payment_failures_total", "Отклонённых платежей")
DB_ERRORS = Counter("app_db_errors_total", "Ошибки при работе с БД", ["operation"])
DB_DURATION = Histogram(
    "app_db_query_duration_seconds",
    "Длительность запросов к БД",
    ["operation"],
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0),
)
DB_READY = Gauge("app_db_ready", "1, если пул соединений с БД готов")

# Хаос: искусственная деградация, чтобы посмотреть на алерты вживую
CHAOS_LATENCY_SECONDS = Gauge("app_chaos_latency_seconds", "Искусственная задержка ответа")
CHAOS_ERROR_RATE = Gauge("app_chaos_error_rate", "Доля искусственных ошибок 5xx")

CHAOS: dict[str, float] = {"latency_ms": 0.0, "error_rate": 0.0}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS orders (
    id          BIGSERIAL PRIMARY KEY,
    item        TEXT        NOT NULL,
    quantity    INTEGER     NOT NULL DEFAULT 1,
    price       NUMERIC(10, 2) NOT NULL,
    status      TEXT        NOT NULL DEFAULT 'created',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

pool: asyncpg.Pool | None = None


# ─────────────────────────────────────────────────────────────────────────────
#  Работа с БД
# ─────────────────────────────────────────────────────────────────────────────


async def connect_with_retry(attempts: int = 30) -> None:
    """Подключаемся в фоне: сервис должен подняться, даже если БД тормозит."""
    global pool

    if not DATABASE_URL:
        LOG.warning("DATABASE_URL не задан — работаю без БД")
        return

    for attempt in range(1, attempts + 1):
        try:
            pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, timeout=5)
            async with pool.acquire() as connection:
                await connection.execute(SCHEMA_SQL)
            DB_READY.set(1)
            LOG.info("Пул соединений с БД готов (попытка %s)", attempt)
            return
        except Exception as exc:  # noqa: BLE001
            DB_ERRORS.labels(operation="connect").inc()
            LOG.warning("БД недоступна (%s/%s): %s", attempt, attempts, exc)
            await asyncio.sleep(2)

    LOG.error("Не удалось подключиться к БД за %s попыток", attempts)


def get_pool() -> asyncpg.Pool:
    if pool is None:
        raise HTTPException(status_code=503, detail="База данных ещё не готова")
    return pool


def row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    data = dict(row)
    if "price" in data:
        data["price"] = float(data["price"])
    if isinstance(data.get("created_at"), datetime):
        data["created_at"] = data["created_at"].isoformat()
    return data


# ─────────────────────────────────────────────────────────────────────────────
#  Приложение
# ─────────────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(connect_with_retry())
    LOG.info("%s запущен", SERVICE_NAME)
    yield
    task.cancel()
    if pool is not None:
        await pool.close()
        LOG.info("Пул соединений закрыт")


app = FastAPI(
    title="Order Service",
    description="Демо-сервис для pet-проекта по мониторингу",
    version="1.0.0",
    lifespan=lifespan,
)


def route_label(request: Request, path: str) -> str:
    """Шаблон пути, а не конкретный URL: /api/orders/42 → /api/orders/{order_id}.

    Иначе каждый новый id создавал бы новую серию метрик, и Prometheus
    «распухал» бы по кардинальности. Несматченные запросы (404, сканеры,
    которые долбятся в /wp-admin) сводим в одну метку — иначе боты могли бы
    раздуть базу метрик до нерабочего состояния.
    """
    route = request.scope.get("route")
    if route is not None:
        return getattr(route, "path", path)
    if request.scope.get("_chaos_hit"):
        return path
    return "<unmatched>"


@app.middleware("http")
async def http_metrics_middleware(request: Request, call_next):
    path = request.url.path

    # Свои же метрики не считаем — иначе RPS на графике завышен
    if path == "/metrics":
        return await call_next(request)

    chaos_eligible = path.startswith("/api/") and not path.startswith("/api/chaos")
    started = time.perf_counter()
    INFLIGHT.inc()

    try:
        if chaos_eligible and CHAOS["latency_ms"] > 0:
            await asyncio.sleep(CHAOS["latency_ms"] / 1000.0)

        if chaos_eligible and CHAOS["error_rate"] > 0 and random.random() < CHAOS["error_rate"]:
            # помечаем, что 500 — наш собственный, иначе метка станет "<unmatched>"
            request.scope["_chaos_hit"] = True
            response: Response = JSONResponse(
                {"detail": "chaos: искусственная ошибка"}, status_code=500
            )
        else:
            response = await call_next(request)
    except Exception:
        REQUEST_DURATION.labels(method=request.method, path=route_label(request, path)).observe(
            time.perf_counter() - started
        )
        REQUEST_TOTAL.labels(method=request.method, path=route_label(request, path), status="500").inc()
        raise
    finally:
        INFLIGHT.dec()

    label = route_label(request, path)
    REQUEST_TOTAL.labels(
        method=request.method, path=label, status=str(response.status_code)
    ).inc()
    REQUEST_DURATION.labels(method=request.method, path=label).observe(
        time.perf_counter() - started
    )
    return response


# ── Служебные ручки ──────────────────────────────────────────────────────────


@app.get("/health", tags=["service"])
async def health() -> dict[str, str]:
    """Liveness: процесс жив. БД здесь намеренно не проверяем."""
    return {"status": "ok", "service": SERVICE_NAME}


@app.get("/ready", tags=["service"])
async def ready() -> dict[str, Any]:
    """Readiness: можем ли реально обслуживать запросы (нужна БД)."""
    if pool is None:
        raise HTTPException(status_code=503, detail="Нет соединения с БД")
    try:
        async with pool.acquire() as connection:
            await connection.fetchval("SELECT 1")
    except Exception as exc:  # noqa: BLE001
        DB_ERRORS.labels(operation="ping").inc()
        raise HTTPException(status_code=503, detail=f"БД не отвечает: {exc}") from exc
    return {"status": "ready"}


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ── Заказы ───────────────────────────────────────────────────────────────────


class OrderIn(BaseModel):
    item: str = Field(min_length=1, max_length=100, examples=["coffee beans"])
    quantity: int = Field(default=1, ge=1, le=100)
    price: float = Field(gt=0, le=100_000, examples=[12.5])


@app.get("/api/orders", tags=["orders"])
async def list_orders(limit: int = 20) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 100))
    started = time.perf_counter()
    try:
        async with get_pool().acquire() as connection:
            rows = await connection.fetch(
                "SELECT id, item, quantity, price, status, created_at "
                "FROM orders ORDER BY id DESC LIMIT $1",
                limit,
            )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        DB_ERRORS.labels(operation="select").inc()
        raise HTTPException(status_code=503, detail="БД недоступна") from exc
    finally:
        DB_DURATION.labels(operation="select").observe(time.perf_counter() - started)

    return [row_to_dict(row) for row in rows]


@app.get("/api/orders/{order_id}", tags=["orders"])
async def get_order(order_id: int) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        async with get_pool().acquire() as connection:
            row = await connection.fetchrow(
                "SELECT id, item, quantity, price, status, created_at "
                "FROM orders WHERE id = $1",
                order_id,
            )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        DB_ERRORS.labels(operation="select_one").inc()
        raise HTTPException(status_code=503, detail="БД недоступна") from exc
    finally:
        DB_DURATION.labels(operation="select_one").observe(time.perf_counter() - started)

    if row is None:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    return row_to_dict(row)


@app.post("/api/orders", status_code=201, tags=["orders"])
async def create_order(order: OrderIn) -> dict[str, Any]:
    # Платежи иногда отклоняются — источник бизнес-метрики и повод для графиков
    payment_failed = random.random() < PAYMENT_FAILURE_RATE
    status = "payment_failed" if payment_failed else "paid"

    started = time.perf_counter()
    try:
        async with get_pool().acquire() as connection:
            row = await connection.fetchrow(
                "INSERT INTO orders (item, quantity, price, status) "
                "VALUES ($1, $2, $3, $4) "
                "RETURNING id, item, quantity, price, status, created_at",
                order.item,
                order.quantity,
                order.price,
                status,
            )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        DB_ERRORS.labels(operation="insert").inc()
        raise HTTPException(status_code=503, detail="БД недоступна") from exc
    finally:
        DB_DURATION.labels(operation="insert").observe(time.perf_counter() - started)

    ORDERS_CREATED.inc()
    if payment_failed:
        PAYMENTS_FAILED.inc()

    return row_to_dict(row) if row is not None else {}


# ── Управляемая деградация ───────────────────────────────────────────────────


@app.get("/api/chaos", tags=["chaos"])
async def get_chaos() -> dict[str, float]:
    return CHAOS


@app.post("/api/chaos", tags=["chaos"])
async def set_chaos(latency_ms: float = 0, error_rate: float = 0) -> dict[str, float]:
    """Включить деградацию: ?latency_ms=800&error_rate=0.2

    Нужно, чтобы посмотреть на алерты и на то, как Alertmanager их группирует,
    не роняя настоящий сервис.
    """
    CHAOS["latency_ms"] = max(0.0, min(latency_ms, 10_000.0))
    CHAOS["error_rate"] = max(0.0, min(error_rate, 1.0))
    CHAOS_LATENCY_SECONDS.set(CHAOS["latency_ms"] / 1000.0)
    CHAOS_ERROR_RATE.set(CHAOS["error_rate"])
    LOG.warning("chaos включён: latency_ms=%s error_rate=%s", CHAOS["latency_ms"], CHAOS["error_rate"])
    return CHAOS


@app.delete("/api/chaos", tags=["chaos"])
async def reset_chaos() -> dict[str, float]:
    CHAOS["latency_ms"] = 0.0
    CHAOS["error_rate"] = 0.0
    CHAOS_LATENCY_SECONDS.set(0)
    CHAOS_ERROR_RATE.set(0)
    LOG.warning("chaos выключен")
    return CHAOS
