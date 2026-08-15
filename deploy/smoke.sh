#!/usr/bin/env bash
# Full-stack deploy smoke test.
#
# Boots the compose stack (default: deploy/docker-compose.yml — the full
# self-hosted variant with all four vector backends), waits for the internal
# admin app's /health to report `status: ok` (Postgres + Redis + worker
# heartbeats + every registered adapter), asserts the public app 404s on
# /health (the admin boundary: probes live on the un-published admin port),
# then runs the real-user API journey across all four backends
# (deploy/smoke/journey.py: register -> collection -> upsert -> query -> filter
# -> hybrid -> async batch via worker + MinIO -> delete, per backend) to prove
# the stack doesn't just boot but actually serves the full API surface. Tears
# the stack down afterwards. Exits non-zero on any failure with a diagnostic
# dump (compose ps + app/worker/milvus logs).
#
# This is the CI deploy gate (`.github/workflows/ci.yml` -> deploy-smoke) and
# runs locally the same way:
#
#   deploy/smoke.sh                 # full self-hosted stack
#   deploy/smoke.sh deploy/docker-compose.cloud.yml   # managed-endpoint variant (needs its env)
#
# Env: HEALTH_TIMEOUT_SECONDS (default 600) bounds the /health wait.
set -euo pipefail

COMPOSE_FILE="${1:-deploy/docker-compose.yml}"
COMPOSE=(docker compose -f "${COMPOSE_FILE}")
HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-600}"

cleanup() {
  echo "=== [smoke] tearing down ==="
  "${COMPOSE[@]}" down -v 2>/dev/null || true
}
trap cleanup EXIT

echo "=== [smoke] building + booting ${COMPOSE_FILE} ==="
"${COMPOSE[@]}" up -d --build

# Read /health from INSIDE the app container: the admin port (9091) is never
# published — reaching it proves the process serves it, exactly like the k8s
# pod probes do.
fetch_health() {
  "${COMPOSE[@]}" exec -T app python - <<'PY' 2>/dev/null || return 1
import json
import urllib.request

try:
    with urllib.request.urlopen("http://127.0.0.1:9091/health", timeout=5) as r:
        print(json.dumps(json.load(r)))
except Exception:
    raise SystemExit(1)
PY
}

echo "=== [smoke] waiting for /health = ok (up to ${HEALTH_TIMEOUT_SECONDS}s) ==="
health=""
status=""
deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
while [ "$SECONDS" -lt "$deadline" ]; do
  health="$(fetch_health || true)"
  if [ -n "$health" ]; then
    status="$(printf '%s' "$health" | python3 -c "import json,sys; print(json.load(sys.stdin).get('status'))")"
    if [ "$status" = "ok" ]; then
      break
    fi
  fi
  echo "  ... not ok yet (${SECONDS}s elapsed)"
  sleep 15
done

if [ "$status" != "ok" ]; then
  echo "=== [smoke] FAILED: /health never reached ok (last: ${health:-no response}) ==="
  "${COMPOSE[@]}" ps
  "${COMPOSE[@]}" logs --tail 60 app worker milvus 2>/dev/null || true
  exit 1
fi
echo "=== [smoke] /health = ok ==="
printf '%s\n' "$health" | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert d['status'] == 'ok', d
assert d['checks']['postgres'] == 'ok' and d['checks']['redis'] == 'ok', d['checks']
assert d['checks']['workers'] == 'ok', d['checks']
adapters = d['checks']['adapters']
assert all(v == 'ok' for v in adapters.values()), adapters
print('  postgres=ok redis=ok workers=ok')
print('  adapters:', ', '.join(f'{k}={v}' for k, v in sorted(adapters.items())))
"

echo "=== [smoke] public /health must 404 (admin boundary) ==="
code="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/health || true)"
if [ "$code" != "404" ]; then
  echo "=== [smoke] FAILED: public /health returned ${code}, expected 404 ==="
  exit 1
fi
echo "  public /health -> 404"

echo "=== [smoke] real-user API journey (all four backends) ==="
# Health proves the stack BOOTS; the journey proves the API WORKS end to end
# through the real production path — register/login, per-backend collection
# create/upsert/query/filter/hybrid, async batch via the arq worker + MinIO
# staging, delete. deploy/smoke/journey.py is stdlib-only, so the runner
# host's python3 suffices.
if ! python3 deploy/smoke/journey.py; then
  echo "=== [smoke] FAILED: API journey ==="
  "${COMPOSE[@]}" ps
  "${COMPOSE[@]}" logs --tail 60 app worker 2>/dev/null || true
  exit 1
fi

echo "=== [smoke] PASSED ==="
