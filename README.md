# System design demo: "Your app just went viral, what breaks first?"

FastAPI + Postgres on k3s, deployed with Argo CD, load tested with k6, watched in Grafana
(metrics: Prometheus, logs: Loki, traces: Tempo).

```
app/            FastAPI service (built to ghcr.io/david-maat/system-design-demo/api by GitHub Actions)
k8s/demo-api/   API Deployment/Service/Ingress  <- demo knobs live here
k8s/postgres/   primary + streaming read replica (StatefulSets)
k8s/k6/         k6 scripts + suspended CronJobs used as job templates
k8s/monitoring/ Grafana dashboard "Your app just went viral"
k8s/shop/       dummy microservices behind /checkout (tracing demo)
k8s/tempo/      Tempo (traces); Loki + Alloy (logs) are Helm apps in argocd/apps/
k8s/cloudflared Cloudflare Tunnel connector + hostname routes (credentials are a hand-made Secret)
argocd/apps/    Argo CD applications (root app is created by bootstrap.yaml)
```

## One-time setup

```bash
# 1. Push the repo so CI builds the image. Then check the GHCR package is public
#    (GitHub -> Packages -> api -> settings -> visibility).

# 2. Cloudflare Tunnel (routes are in k8s/cloudflared/config.yaml)
cloudflared tunnel login                         # pick the davidmaat.nl zone
cloudflared tunnel create system-design-demo     # prints TUNNEL_ID, writes ~/.cloudflared/<TUNNEL_ID>.json
for h in demo grafana argocd; do cloudflared tunnel route dns system-design-demo $h.davidmaat.nl; done

# 3. Bootstrap: Argo CD + root app + all secrets in one file (bootstrap.yaml is gitignored)
cp bootstrap.example.yaml bootstrap.yaml && $EDITOR bootstrap.yaml
kubectl apply -f bootstrap.yaml
```

Public endpoints:

| URL | What | Auth |
|---|---|---|
| https://demo.davidmaat.nl | the API (`/`, `/compute`, `/items/stats`) | none |
| https://grafana.davidmaat.nl | dashboard "Your app just went viral" | anonymous viewer; admin login for edits |
| https://argocd.davidmaat.nl | Argo CD UI | Argo CD login (consider Cloudflare Access too) |

Never point k6 at these URLs; load stays inside the cluster.
To add a hostname, add it to `k8s/cloudflared/config.yaml` and run `cloudflared tunnel route dns`.

## Demo script

Load always comes from **inside the cluster**, never through Cloudflare.

**Automated:** `./demo.sh` runs both acts in order (Enter moves to the next step, `--auto` runs on a timer,
`--reset` only resets). It changes the knobs with kubectl while Argo CD is paused for `demo-api` and `postgres`,
and resets everything on exit. The manual Git-based steps below still work.

### Act 1: one server gets overloaded → scale out
```bash
kubectl -n demo create job --from=cronjob/k6-overload k6-overload-$(date +%s)
```
On the dashboard, API CPU is pinned at 0.2, p95 latency climbs and served req/s stays below offered req/s.
Edit `k8s/demo-api/deployment.yaml` to set `replicas: 1` → `4`, commit and push, then refresh in Argo CD.
New pods appear, "Requests/s per API pod" splits the traffic (load balancing) and latency drops.

### Act 2: the database becomes the bottleneck → read replica
```bash
kubectl -n demo create job --from=cronjob/k6-reads k6-reads-$(date +%s)
```
Primary Postgres CPU is pinned at 0.5 and `/items/stats` latency explodes. Adding API pods doesn't help.
In `k8s/demo-api/kustomization.yaml`, switch `READ_DATABASE_URL` to the replica line, then commit and push.
Reads move to the replica, primary CPU drops and only writes remain on it.
Bonus: set `replicas: 2` in `k8s/postgres/replica.yaml` to scale reads further.
Talking point: with more API pods, `DB_POOL_SIZE × pods` connections add up (connection pooling, PgBouncer).

### Logs and traces
The API sends a trace per request (OpenTelemetry, with a span per SQL query) to Tempo and logs one line per
request with its `trace_id`; Alloy ships the `demo` namespace's logs to Loki. On the dashboard:
- **Service graph**: Act 2 shows the read traffic move from `postgres-primary` to `postgres-replica`.
- **Slowest recent requests**: click a trace to see where the time goes (CPU in `/compute`, the `SELECT` in `/items/stats`).
- **Distributed trace**: `curl https://demo.davidmaat.nl/checkout` calls the dummy shop services
  (`k8s/shop/`) one after another: inventory → pricing → payment (→ fraud-check) → shipping. Open the trace:
  the waterfall shows the calls are sequential and that payment + fraud-check eat most of the time
  (talking point: run independent calls in parallel). Payment fails ~3% of the time; the error shows up red
  in the trace and in the service graph.
- **API logs**: click `trace_id` on a line to open its trace; from a trace, "Logs for this span" goes back.

### Reset between rehearsals
Revert the commits, then `kubectl -n demo delete job -l job-name` (or let the TTL clean up).

## Quick checks from your laptop (optional)
```bash
k6 run -e TARGET=http://192.168.4.1 -e RATE=30 k8s/k6/scripts/overload.js
```

## Tuning
If the cluster is faster or slower than expected, adjust `RATE` (k6 env), `HASH_ROUNDS` (API config)
or the CPU limits so that one pod/DB clearly saturates and the scaled version clearly doesn't.
