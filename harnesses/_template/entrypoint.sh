#!/usr/bin/env bash
# EDIT-ME product — runner entrypoint. Runs inside the playwright container
# (see Containerfile.playwright). Responsibilities, in order:
#
#   1. loopback forwards/proxies if the suite hardcodes localhost URLs
#      (see quay-oidc's config-proxy.mjs for a /config-rewriting example)
#   2. fixture warmup that must happen BEFORE the suite (see quay-oidc's
#      warmup-oidc.mjs: log every OIDC user in through the real flow so
#      no other signin can poison the external-identity bindings)
#   3. exec "$@"  (the last line — run.py appends `npx playwright test ...`)
set -euo pipefail

exec "$@"
