#!/usr/bin/env bash
# Entrypoint for the OIDC Playwright runner.
#
# 1. Wait for Keycloak discovery and Quay's health endpoint.
# 2. Forward localhost:8080 -> quay:8080 and localhost:8081 -> quay-keycloak:8080
#    so the suite can keep using its default localhost URLs. The 8080 forward
#    is a /config-rewriting proxy (config-proxy.mjs), not plain socat.
# 3. Run the provided command (default: the full suite minus the upgrade lane).
# 4. Produce a host-readable summary from the JSON reporter output.
set -u

QUAY_SERVICE_HOST="${QUAY_SERVICE_HOST:-quay}"
QUAY_SERVICE_PORT="${QUAY_SERVICE_PORT:-8080}"
KC_SERVICE_HOST="${KC_SERVICE_HOST:-quay-keycloak}"
KC_SERVICE_PORT="${KC_SERVICE_PORT:-8080}"

wait_for() {
  local url="$1" name="$2"
  echo "[entrypoint] waiting for ${name} at ${url}"
  for _ in $(seq 1 180); do
    if curl -fsS --max-time 3 "$url" >/dev/null 2>&1; then
      echo "[entrypoint] ${name} is ready"
      return 0
    fi
    sleep 2
  done
  echo "[entrypoint] ERROR: ${name} did not become ready at ${url}" >&2
  return 1
}

# Keycloak must be serving discovery before Quay boots, otherwise /config may
# not advertise the external login provider.
wait_for "http://${KC_SERVICE_HOST}:${KC_SERVICE_PORT}/realms/quay/.well-known/openid-configuration" "Keycloak" || exit 1
wait_for "http://${QUAY_SERVICE_HOST}:${QUAY_SERVICE_PORT}/health/instance" "Quay" || exit 1

start_forward() {
  # Background socat; IPv6 listeners are best-effort (the container may have no
  # IPv6 loopback, and Node/Chromium may still resolve localhost to 127.0.0.1).
  socat "$1" "$2" >/dev/null 2>&1 &
}

# localhost:8080 goes through the /config-rewriting proxy: identical
# pass-through, except GET /config gains a *_LOGIN_CONFIG entry so the
# bearer-token and multi-issuer specs can discover the OIDC provider (Quay's
# /config never exposes it; RCA-20261023-143827 §6.5). socat keeps serving
# 8081 (Keycloak), which needs no rewriting.
node /usr/local/bin/config-proxy.mjs >/tmp/config-proxy.log 2>&1 &
start_forward "TCP-LISTEN:8081,bind=127.0.0.1,reuseaddr,fork" "TCP:${KC_SERVICE_HOST}:${KC_SERVICE_PORT}"
start_forward "TCP6-LISTEN:8081,ipv6only=1,reuseaddr,fork" "TCP:${KC_SERVICE_HOST}:${KC_SERVICE_PORT}"

# Wait for the IPv4 forward (the one the suite depends on most).
for _ in $(seq 1 30); do
  if curl -fsS --max-time 2 "http://127.0.0.1:${QUAY_SERVICE_PORT}/health/instance" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

# Bind every OIDC test user to the real provider (sub claim) before the suite
# starts. See warmup-oidc.mjs for why this must precede any password signin.
if [ "${OIDC_WARMUP:-1}" = "1" ] && [ -f /app/warmup-oidc.mjs ]; then
  node /app/warmup-oidc.mjs || exit 1
fi

set +e
"$@"
status=$?

# Always produce the summary when Playwright wrote its JSON report.
if [ -f /app/test-results/results.json ]; then
  node /usr/local/bin/summarize.mjs || true
fi

exit "${status}"
