#!/usr/bin/env bash
# Runs the whole talk end to end: overload -> scale out -> DB bottleneck -> read replicas.
#
#   ./demo.sh           press Enter to move to each next step (for presenting)
#   ./demo.sh --auto    timed, no input needed (for rehearsing)
#   ./demo.sh --reset   only put the cluster back to the starting state
#
# Knobs are changed with kubectl, not Git. While the script runs, Argo CD is told to
# skip the demo-api and postgres apps (so selfHeal doesn't undo the changes). On exit,
# including Ctrl-C, everything is reset and Argo CD takes over again.
set -euo pipefail

NS=demo
APPS=(demo-api postgres)
REPLICA_URL=postgresql://demo:demo-password@postgres-replica:5432/demo
API_PODS=4          # Act 1: one pod serves ~8 req/s, k6 offers 24
DB_REPLICAS=3       # Act 2: one Postgres serves ~3-4 heavy reads/s, k6 offers ~7

AUTO=false
case "${1:-}" in
  --auto) AUTO=true ;;
  --reset) RESET_ONLY=true ;;
  "") ;;
  *) echo "usage: $0 [--auto|--reset]"; exit 1 ;;
esac

k() { kubectl -n "$NS" "$@"; }
say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

# step <seconds in --auto mode> <what to look at on the dashboard>
step() {
  printf '\033[33m    watch: %s\033[0m\n' "$2"
  if $AUTO; then
    for ((s = $1; s > 0; s--)); do printf '\r    next step in %3ds ' "$s"; sleep 1; done; echo
  else
    read -rp "    [Enter] to continue " _
  fi
}

argo_pause() {
  for a in "${APPS[@]}"; do
    kubectl -n argocd annotate application "$a" argocd.argoproj.io/skip-reconcile=true --overwrite >/dev/null
  done
}
argo_resume() {
  for a in "${APPS[@]}"; do
    kubectl -n argocd annotate application "$a" argocd.argoproj.io/skip-reconcile- >/dev/null 2>&1 || true
  done
}

stop_load() { k delete job -l 'job-name' --ignore-not-found --wait=false >/dev/null; }
start_load() { k create job --from="cronjob/$1" "$1-$(date +%s)" >/dev/null; echo "    started k6 job from cronjob/$1"; }

reset() {
  say "Reset: stop load, 1 API pod, reads on primary, 1 read replica"
  stop_load
  k set env deploy/demo-api READ_DATABASE_URL- >/dev/null
  k scale deploy/demo-api --replicas=1 >/dev/null
  k scale statefulset/postgres-replica --replicas=1 >/dev/null
  k rollout status deploy/demo-api --timeout=180s
  k rollout status statefulset/postgres-replica --timeout=300s
}

cleanup() {
  trap - EXIT INT TERM
  reset || echo "reset failed, check the cluster"
  argo_resume
  say "Done. Argo CD manages demo-api and postgres again."
}

argo_pause
trap cleanup EXIT INT TERM
[[ "${RESET_ONLY:-}" == true ]] && exit 0
reset

# ------------------------------------------------------------------ Act 1
say "Act 1: the app goes viral. One API pod gets 3x the traffic it can serve"
start_load k6-overload
step 120 "API CPU pinned at 0.2, p95 latency climbs, served req/s < offered req/s"

say "Scale out: 1 -> $API_PODS API pods"
k scale deploy/demo-api --replicas="$API_PODS"
k rollout status deploy/demo-api --timeout=180s
step 120 "'Requests/s per API pod' splits the traffic, latency drops"
stop_load

# ------------------------------------------------------------------ Act 2
say "Act 2: read-heavy traffic. Now the database is the bottleneck"
start_load k6-reads
step 120 "primary Postgres CPU pinned at 0.5, /items/stats latency explodes (more API pods don't help)"

say "Send reads to the read replica"
k set env deploy/demo-api READ_DATABASE_URL="$REPLICA_URL"
k rollout status deploy/demo-api --timeout=180s
step 90 "reads move to the replica, primary CPU drops; one replica is now the hot spot"

say "Scale reads: 1 -> $DB_REPLICAS read replicas"
k scale statefulset/postgres-replica --replicas="$DB_REPLICAS"
k rollout status statefulset/postgres-replica --timeout=600s
step 120 "load spreads over the replicas, /items/stats latency recovers (talking point: DB_POOL_SIZE x pods connections)"
# cleanup (trap) resets everything
