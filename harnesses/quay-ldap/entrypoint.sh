#!/usr/bin/env bash
# Entrypoint for the LDAP Playwright runner.
#
# 1. Wait for Quay's health endpoint.
# 2. Forward localhost:8080 -> quay:8080 so the suite keeps using its default
#    localhost URL (SERVER_HOSTNAME is localhost:8080).
# 3. Run the provided command (default: the @auth:LDAP specs).
#
# Unlike the OIDC harness there is no config-proxy: Quay's /config advertises
# AUTHENTICATION_TYPE: LDAP, which is all the sign-in specs need.
set -u

QUAY_SERVICE_HOST="${QUAY_SERVICE_HOST:-quay}"
QUAY_SERVICE_PORT="${QUAY_SERVICE_PORT:-8080}"

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

wait_for "http://${QUAY_SERVICE_HOST}:${QUAY_SERVICE_PORT}/health/instance" "Quay" || exit 1

start_forward() {
  # Background socat; IPv6 listeners are best-effort (the container may have no
  # IPv6 loopback, and Node/Chromium may still resolve localhost to 127.0.0.1).
  socat "$1" "$2" >/dev/null 2>&1 &
}

start_forward "TCP-LISTEN:8080,bind=127.0.0.1,reuseaddr,fork" "TCP:${QUAY_SERVICE_HOST}:${QUAY_SERVICE_PORT}"
start_forward "TCP6-LISTEN:8080,ipv6only=1,reuseaddr,fork" "TCP:${QUAY_SERVICE_HOST}:${QUAY_SERVICE_PORT}"

exec "$@"
