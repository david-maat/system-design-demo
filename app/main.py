"""Demo API for the 'Your app just went viral' talk.

Endpoints
  GET  /compute       CPU-bound work -> shows a single pod saturating (scale app)
  GET  /items/stats   heavy read query -> shows the primary DB saturating (read replica)
  GET  /items/{id}    cheap read
  POST /items         write, always goes to the primary
  GET  /checkout      calls the dummy shop services in a row -> shows a distributed trace
"""
import hashlib
import os
import random
import socket

import httpx
from fastapi import FastAPI, HTTPException
from prometheus_fastapi_instrumentator import Instrumentator
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

import telemetry

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

log = telemetry.setup("demo-api")
if telemetry.TRACING:
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor

    # Must run before the pools open connections: every SQL query becomes a span.
    PsycopgInstrumentor().instrument(enable_commenter=False)

pool_opts = dict(min_size=1, max_size=POOL_SIZE, max_lifetime=CONN_MAX_LIFETIME, open=False)
primary = ConnectionPool(PRIMARY_URL, **pool_opts)
replica = (
    primary
    if READ_URL == PRIMARY_URL
    else ConnectionPool(READ_URL, **pool_opts)
)
# /checkout calls these one after another (comma separated URLs).
CHECKOUT_SERVICES = [u for u in os.environ.get("CHECKOUT_SERVICES", "").split(",") if u]
http = httpx.Client(timeout=5)

app = FastAPI(title="system-design-demo")
Instrumentator().instrument(app).expose(app)
telemetry.instrument_app(app)
telemetry.add_access_log(app, log)


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


@app.get("/checkout")
def checkout():
    # Sequential on purpose: total time = sum of all calls. The trace waterfall shows
    # which service is slow (talking point: independent calls could run in parallel).
    steps = []
    for url in CHECKOUT_SERVICES:
        r = http.get(url)
        if r.status_code >= 400:
            log.warning("checkout failed at %s: %d", url, r.status_code)
            raise HTTPException(502, f"checkout failed at {url}")
        steps.append(r.json())
    log.info("checkout ok, %d services called", len(steps))
    return {"pod": HOSTNAME, "steps": steps}
