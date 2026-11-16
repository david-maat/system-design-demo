"""A fake microservice for the tracing demo (inventory, pricing, payment, ...).

One image, configured per Deployment:
  OTEL_SERVICE_NAME  name shown in traces and logs
  WORK_MS            simulated work in ms (+-30% jitter)
  DOWNSTREAM         comma separated URLs, called one after another after the work
  ERROR_RATE         fraction of requests that fail with a 500 (0.0 - 1.0)
"""
import os
import random
import time

import httpx
from fastapi import FastAPI, HTTPException

import telemetry

NAME = os.environ.get("OTEL_SERVICE_NAME", "dummy")
WORK_MS = float(os.environ.get("WORK_MS", "50"))
DOWNSTREAM = [u for u in os.environ.get("DOWNSTREAM", "").split(",") if u]
ERROR_RATE = float(os.environ.get("ERROR_RATE", "0"))

log = telemetry.setup(NAME)
http = httpx.Client(timeout=5)
app = FastAPI(title=NAME)
telemetry.instrument_app(app)
telemetry.add_access_log(app, log)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/")
def work():
    time.sleep(WORK_MS * random.uniform(0.7, 1.3) / 1000)  # pretend to do something
    calls = []
    for url in DOWNSTREAM:
        r = http.get(url)
        if r.status_code >= 400:
            log.warning("downstream %s failed: %d", url, r.status_code)
            raise HTTPException(502, f"{NAME}: downstream {url} failed")
        calls.append(r.json())
    if random.random() < ERROR_RATE:
        log.error("%s failed (simulated)", NAME)
        raise HTTPException(500, f"{NAME} failed")
    return {"service": NAME, "calls": calls}
