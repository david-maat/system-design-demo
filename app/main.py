"""Demo API for the 'Your app just went viral' talk.

Endpoints
  GET  /compute       CPU-bound work -> shows a single pod saturating (scale app)
  GET  /items/stats   heavy read query -> shows the primary DB saturating (read replica)
  GET  /items/{id}    cheap read
  POST /items         write, always goes to the primary
"""
import hashlib
import logging
import os
import random
import socket
import time

from fastapi import FastAPI, HTTPException, Request
from prometheus_fastapi_instrumentator import Instrumentator
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

PRIMARY_URL = os.environ["DATABASE_URL"]
# When READ_DATABASE_URL is empty, reads go to the primary as well.
READ_URL = os.environ.get("READ_DATABASE_URL") or PRIMARY_URL
POOL_SIZE = int(os.environ.get("DB_POOL_SIZE", "5"))
# Kubernetes Services balance per TCP connection, not per query. Recycling pooled
# connections regularly spreads them over newly added read replicas.
CONN_MAX_LIFETIME = float(os.environ.get("DB_CONN_MAX_LIFETIME", "15"))
HASH_ROUNDS = int(os.environ.get("HASH_ROUNDS", "20000"))
HOSTNAME = socket.gethostname()
CATEGORIES = ["books", "games", "music", "movies", "tools", "garden", "toys", "food"]

# ---- Logs + traces -----------------------------------------------------------
# Every log line carries trace_id=..., so Grafana can jump from a log line in Loki
# to its trace in Tempo. Traces are only exported when OTEL_EXPORTER_OTLP_ENDPOINT is set.
from opentelemetry.instrumentation.logging import LoggingInstrumentor

TRACING = bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"))
if TRACING:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    # Service name comes from OTEL_SERVICE_NAME; tag spans with the pod as well.
    provider = TracerProvider(resource=Resource.create({"service.instance.id": HOSTNAME}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    # Must run before the pools open connections: every SQL query becomes a span
    # with server.address = postgres-primary or postgres-replica.
    PsycopgInstrumentor().instrument(enable_commenter=False)

LoggingInstrumentor().instrument(set_logging_format=True)
log = logging.getLogger("demo-api")

pool_opts = dict(min_size=1, max_size=POOL_SIZE, max_lifetime=CONN_MAX_LIFETIME, open=False)
primary = ConnectionPool(PRIMARY_URL, **pool_opts)
replica = (
    primary
    if READ_URL == PRIMARY_URL
    else ConnectionPool(READ_URL, **pool_opts)
)

app = FastAPI(title="system-design-demo")
Instrumentator().instrument(app).expose(app)
if TRACING:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(
        app, excluded_urls="healthz,metrics", exclude_spans=["send", "receive"]
    )


# One log line per request (uvicorn's access log is off, it can't carry trace ids).
@app.middleware("http")
async def access_log(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    if request.url.path not in ("/healthz", "/metrics"):
        log.info(
            "%s %s %d %.0fms",
            request.method, request.url.path, response.status_code,
            (time.perf_counter() - start) * 1000,
        )
    return response


@app.on_event("startup")
def startup() -> None:
    primary.open()
    if replica is not primary:
        replica.open()


# async -> runs on the event loop, not in the threadpool that /compute saturates,
# so health checks still answer while the pod is overloaded.
@app.get("/healthz")
async def healthz():
    return {"ok": True, "pod": HOSTNAME}


@app.get("/")
def root():
    return {
        "pod": HOSTNAME,
        "reads_from": "replica" if replica is not primary else "primary",
    }


@app.get("/compute")
def compute():
    # Sync endpoint -> runs in the threadpool; pure CPU, bounded by the pod's CPU limit.
    h = str(random.random()).encode()
    for _ in range(HASH_ROUNDS):
        h = hashlib.sha256(h).digest()
    return {"pod": HOSTNAME, "hash": h.hex()[:16]}


@app.get("/items/stats")
def item_stats(category: str | None = None):
    category = category or random.choice(CATEGORIES)
    # Deliberately unindexed aggregate -> sequential scan, CPU heavy on the DB.
    with replica.connection() as conn:
        row = conn.execute(
            "SELECT count(*), avg(price)::float, max(price)::float "
            "FROM items WHERE category = %s AND description LIKE %s",
            (category, "%" + random.choice("abcdef") + "%"),
        ).fetchone()
    return {"pod": HOSTNAME, "category": category, "count": row[0], "avg": row[1], "max": row[2]}


@app.get("/items/{item_id}")
def get_item(item_id: int):
    with replica.connection() as conn:
        row = conn.execute(
            "SELECT id, name, category, price::float FROM items WHERE id = %s", (item_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404)
    return dict(zip(["id", "name", "category", "price"], row))


class NewItem(BaseModel):
    name: str
    category: str = "books"
    price: float = 9.99


@app.post("/items", status_code=201)
def create_item(item: NewItem):
    with primary.connection() as conn:
        row = conn.execute(
            "INSERT INTO items (name, category, price, description) VALUES (%s, %s, %s, %s) RETURNING id",
            (item.name, item.category, item.price, item.name),
        ).fetchone()
    return {"id": row[0], "pod": HOSTNAME}
