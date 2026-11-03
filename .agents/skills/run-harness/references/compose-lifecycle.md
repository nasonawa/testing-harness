# Compose lifecycle reference

Per-command notes for `./run.sh <command>`. `run.sh` is a shim; the real
orchestrator is run.py (stdlib Python 3). All commands accept env via
`.env`, the environment, or defaults — `doctor` prints the resolution.

## build

- Source mode (`QUAY_REPO_ROOT` set): builds the product image from the
  checkout (tag `QUAY_BUILT_IMAGE`, deliberately different from
  `QUAY_IMAGE` so builds never shadow the published ref) and the runner.
- Pull mode: pulls `QUAY_IMAGE`. The runner build needs the checkout;
  run.py warns if it is missing — `test` will fail fast.

## up

Starts dependencies first (healthcheck-gated), then the product, waiting
for readiness. First boot may run migrations — the quay healthcheck
allows several minutes. Idempotent: re-running after a code change needs
a rebuild first (source mode) — `up` alone never rebuilds images.

## verify

Runs verify.py on the host against the running stack, writing
`reports/runs/<ts>/verify.md` and updating `reports/latest`. Exit code
reflects required checks only. `--since` semantics: log checks scan from
the verify start time by default (older leaks from previous runs don't
count — rerun `verify` to re-trigger the logins it exercises).

## test

`compose run --rm playwright` with the profile enabled; extra args after
`--` go to the test runner (`npx playwright test ...`). Requires
`QUAY_REPO_ROOT` (suite bind-mounts). Output tees into
`reports/runs/<ts>[-label]/logs/`. Use `RUN_LABEL` to name the directory.
Runner images pin the Playwright version from the checkout's lockfile —
rebuild the runner when the checkout moves.

## shell

Bash in the runner container (profile started on demand) — for poking at
the stack from inside the compose network.

## logs

`docker compose logs -f [service]`. With no argument, everything.

## down / clean

- `down`: stop containers, keep volumes (Quay DB, Keycloak data...) and
  reports. Safe default.
- `clean`: `down -v` + removes `reports/`. Destroys fixture identity
  state — in quay-oidc this resets the OIDC user bindings and the first
  `up`+`test` after it re-establishes them (expect a slower, noisier
  first run). Read the harness README before running it.

## doctor

Side-effect-free. Prints mode, images, DOCKER_USER, repo root presence,
compose file list. Run it first, always — and include its output in bug
reports alongside the report directory path.
