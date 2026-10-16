#!/usr/bin/env bash
# Disposable full-stack smoke test for CI: build, migrate, start, wait for /ready, then check the
# bundled UI, liveness, request-id correlation and one seed-then-read flow. Requires a unique
# COMPOSE_PROJECT_NAME and a .env file; the workflow always tears the project down afterwards.
set -euo pipefail

API_URL="http://127.0.0.1:8000"
WAIT_TIMEOUT_SEC="${WAIT_TIMEOUT_SEC:-120}"

fail() {
  echo "stack smoke failed: $*" >&2
  exit 1
}

make build
make migrate
docker compose up -d

# Dependency-aware readiness: /ready also checks Postgres, Redis and Qdrant; /health does not.
echo "Waiting up to ${WAIT_TIMEOUT_SEC}s for $API_URL/ready ..."
ready=false
for i in $(seq 1 "$WAIT_TIMEOUT_SEC"); do
  if curl -fsS "$API_URL/ready" >/dev/null 2>&1; then
    ready=true
    echo "API ready after ${i}s"
    break
  fi
  sleep 1
done
$ready || fail "/ready not OK after ${WAIT_TIMEOUT_SEC}s"

# Bundled UI: the React shell comes from the frontend build baked into the api image.
page=$(curl -fsS "$API_URL/ui/")
grep -q '<div id="root"' <<<"$page" || fail "UI shell missing at /ui/"

# Request correlation: a well-formed X-Request-ID must be echoed on the response.
headers=$(curl -fsS -D - -o /dev/null -H 'X-Request-ID: edg070-smoke' "$API_URL/health")
grep -qi '^x-request-id: edg070-smoke' <<<"$headers" || fail "X-Request-ID not echoed by /health"

# Smallest supported flow without a provider call: seed the solo campaign, then read its session.
seed=$(curl -fsS -X POST "$API_URL/api/seed")
session_id=$(jq -er '.session_id' <<<"$seed") || fail "seed response has no session_id"
session=$(curl -fsS "$API_URL/api/sessions/$session_id")
jq -e --argjson id "$session_id" '.id == $id' <<<"$session" >/dev/null || fail "session read mismatch"

echo "Stack smoke passed: ready, UI, health, request id, seed and session read"
