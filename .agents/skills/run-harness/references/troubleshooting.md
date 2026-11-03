# Troubleshooting

Symptom → cause → fix. Always run `./run.sh doctor` first and compare its
output against what you expect; many of these show up right there.

## `unable to setup user: chown /dev/stdin: invalid argument` (runc)

Rootless Docker/Podman: the host uid is outside the container's subuid
range. run.py auto-detects this (docker info SecurityOptions contains
"rootless" → DOCKER_USER=0:0). If you see it anyway, something bypassed
run.py — run the compose command through `./run.sh`, or set DOCKER_USER=0:0.
Rootful engines need the real uid:gid instead (auto-detected too).

## Runner/test failures right after a product checkout update

The runner image pins the suite's Playwright version from the checkout's
lockfile at build time. Rebuild: `QUAY_REPO_ROOT=... ./run.sh build`.
Symptom shape: suite errors about missing browser binaries or version
mismatch between @playwright/test and the installed browsers.

## Green verify, red suite (or vice versa)

They test different things: verify.py gates the specific behavior the
harness exists for; the suite is broader. A red suite with a green gate is
usually fixture drift (users/config changed) or runner problems — check
`reports/latest/logs/` before touching anything.

## Pull-mode gate fails while the fix definitely works

Image/suite skew: the published `QUAY_IMAGE` predates the fix. Source
mode (`QUAY_REPO_ROOT` pointing at a checkout containing the fix) is the
mode the gate is meaningful in; check the harness README for the last
image tag the gate was validated against.

## quay-oidc: mass auth failures after `clean` (or volume wipe)

Expected once, broken if persistent: `clean` wipes the `keycloak-data`
volume AND the Quay DB together; the first `up`+`test` re-imports the
realm and re-establishes the OIDC identity bindings (warmup logs every
user in through the real flow first). If failures persist past the first
full run: the compose project name was changed (breaking named-volume
reuse), or someone deleted only ONE of the two states — see the
"Keycloak identity stability" section of the quay-oidc README.

## quay-oidc: a user's OIDC login resolves to the wrong identity

The username was bound to a non-OIDC (database) identity first — usually
a password signin during manual poking. Fix: `./run.sh clean` and let the
next full run re-establish bindings (costly but reliable).

## `unknown docker command: "compose ..."` style errors

The compose invocation is broken, not the stack — if it mentions a
compose file name as a subcommand, the `-f` flags were dropped (a bug we
shipped once); fix compose_base() in run.py. If it says compose is
unknown at all: old Docker or podman without the compose subcommand —
set DOCKER=podman (podman-compose users) or upgrade.

## Ports already in use

Host ports are `<PRODUCT>_HOST_PORT`-style env vars (quay-ldap: 18090,
quay-oidc: 18080/18081...) — chosen off the product's own dev ports. If
something else squats on them, override in `.env`, don't edit compose.

## OCP: verify Job finds no logs / no pods

`<PRODUCT>_POD_LABEL` does not match the operator's labeling. Inspect:
`oc get pods -n <namespace> --show-labels`, then set the label env and
re-run `./run.sh ocp verify`.

## OCP: apply succeeds, registry never Available

`oc describe <CR> -n <namespace>`. Usual cause: a config-bundle key the
operator manages was not stripped from `ocp/config-bundle.yaml`.

## Stale `ocp/generated/` or weird kustomize errors

Generated manifests are rewritten by every `ocp` subcommand; if you
applied them by hand with `oc apply` and hit missing-file errors, that is
the load restrictor doing its job — go through `./run.sh ocp` only.
