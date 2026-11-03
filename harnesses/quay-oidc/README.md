# quay-oidc — OIDC authentication harness for Quay

Self-contained Docker Compose environment that runs Quay configured for OIDC
against Keycloak, with the full CI-parity service set: Clair, Jaeger, Garage
S3 with two geo-replicated regions, the repo-mirror worker, build support
(PopenExecutor + quay-builder) and Mailpit. The Playwright runner executes
the quay e2e suite against it.

## Architecture

```
                ┌────────────────────────────────────────────────────┐
                │               docker compose network               │
                │                                                    │
  host:18080 ───┼─> quay (Quay, OIDC auth) ── clair (netns-shared)   │
                │     │        │              repomirror (netns)     │
                │     │        └── quay-keycloak (realm: quay)       │
  host:18081 ───┼──────────────────┘   (pinned user UUIDs,           │
                │                          keycloak-data volume)     │
                │     ├── quay-db / clair-db (PostgreSQL 18, pg_trgm)│
                │     ├── quay-redis                                 │
                │     ├── garage (S3, 2 geo-rep regions)             │
                │     ├── jaeger (OTEL)                              │
                │     └── quay-mailpit                               │
                │                                                    │
                │  playwright runner (loopback proxies 8080/8081)    │
                └────────────────────────────────────────────────────┘
```

The runner reaches Quay and Keycloak through loopback forwards
(`entrypoint.sh`) so the suite keeps using `http://localhost:8080` and
`http://localhost:8081`. The 8080 forward is a `/config`-rewriting proxy
(`config-proxy.mjs`) that injects the `SOMEOIDC_LOGIN_CONFIG` entry Quay's
`/config` never exposes, so the bearer-token and multi-issuer specs can
discover the provider.

## Quick start

```bash
cd harnesses/quay-oidc
cp .env.example .env          # optional; edit to taste

# Pull mode (default): test a published image.
QUAY_IMAGE=quay.io/projectquay/quay:<tag> ./run.sh up

# Source mode: build Quay from a local checkout (also enables build support).
QUAY_REPO_ROOT=/path/to/quay ./run.sh build
QUAY_REPO_ROOT=/path/to/quay ./run.sh up
```

`up` starts everything Quay depends on, initializes Garage (layout, buckets,
fixed access key), then Quay, Clair and the repo-mirror worker, waiting for
each. First boot runs database migrations (the healthcheck allows 300s).

`test` requires `QUAY_REPO_ROOT` in either mode (the suite is bind-mounted
from `<QUAY_REPO_ROOT>/web`):

```bash
QUAY_REPO_ROOT=/path/to/quay ./run.sh build     # runner image (fetches CLIs)
QUAY_REPO_ROOT=/path/to/quay ./run.sh test      # full suite minus upgrade lane
RUN_LABEL=smoke QUAY_REPO_ROOT=/path/to/quay ./run.sh test -- --grep "@auth:OIDC"
```

In pull mode, build specs are not runnable (no quay-builder binary — run.py
warns); grep-invert them or use source mode.

## Keycloak identity stability (read before `clean`)

Quay binds an external identity on **first login**, keyed by the OIDC `sub`
claim. Three things keep that stable:

1. the `keycloak-data` volume — without it every `up` re-imports the realm
   with freshly generated user UUIDs that no longer match the logins stored
   in PostgreSQL (this broke 610 tests once; see the RCA history in the
   quay repo),
2. the pinned user UUIDs in `keycloak/quay-realm.json`,
3. `warmup-oidc.mjs` — the entrypoint logs every OIDC test user in through
   the real browser flow *before* the suite starts, so no
   `RawApiClient` password signin can bind the username to a non-OIDC
   identity first.

`./run.sh clean` (`down -v`) wipes that state: the DB and Keycloak must
reset together, and the first `up`+`test` afterwards re-establishes the
bindings. Do not delete just one of the two.

## Commands

| Command | What it does |
|---|---|
| `./run.sh build` | Fetch registry CLIs, then build quay+runner (source) / pull `QUAY_IMAGE` |
| `./run.sh fetch [--force]` | Download crane/regctl/oras/cosign into `./bin` |
| `./run.sh up` | Start the full stack, wait for readiness |
| `./run.sh verify` | Light health/issuer checks (the real gate is `test`) |
| `./run.sh test [args]` | Playwright suite; default: all minus `@upgrade-seed\|@upgrade-verify` |
| `./run.sh report` | Regenerate `summary.md` from the latest run's results.json |
| `./run.sh shell` | Bash in the runner container |
| `./run.sh logs [svc]` | Follow compose logs |
| `./run.sh down` / `clean` | Stop / stop + wipe volumes and reports (see above) |
| `./run.sh doctor` | Print resolved config; run this first when debugging |
| `./run.sh ocp …` | OpenShift variant (below) |

## Reports

Each `test` writes `reports/runs/<YYYYMMDD-HHMMSS>[-<label>]/` with
`playwright-report/` (HTML), `test-results/` (JSON + traces),
`summary/summary.md` (per-auth-tag grouping by `summarize.mjs`) and
`logs/{playwright,quay}.log`; `reports/latest` symlinks to the newest run.
Under OIDC, `@auth:Database` and `@auth:LDAP` specs auto-skip (auth-aware
fixtures); the compatibility analysis lives in the quay repo's
`.cursor/oidc-testing/oidc-compatibility-report.md`.

## Users and credentials

Keycloak realm `quay` (admin console `admin`/`admin`), all user passwords
`password`: `admin_oidc` (superuser), `testuser_oidc`, `readonly_oidc`
(global readonly superuser), `sync_oidc` (group sync). UUIDs are pinned in
`keycloak/quay-realm.json`. All dev-only fixtures — see the repo AGENTS.md
security note.

## OpenShift variant (operator-installed Quay)

`ocp/` deploys the same scenario through the quay-operator: namespace
`quay-oidc-harness`, on-cluster Keycloak (realm single-sourced from
`keycloak/quay-realm.json` via a run.py-generated ConfigMap), a
`QuayRegistry` with a config bundle whose OIDC endpoints point at the
in-cluster Keycloak service, and a verify Job running **the same
`verify.py`** (health, discovery-issuer match, no-secrets-in-logs) with
`--log-source ocp`.

Prerequisites: the quay-operator installed via OLM, `oc` logged in.

```bash
./run.sh ocp apply    # ~10-15 min; waits for the QuayRegistry
./run.sh ocp verify   # runs the verify Job against the Quay Route
./run.sh ocp logs     # follow the quay-app deployment logs
./run.sh ocp delete   # remove everything
```

The browser suite is compose-only. Status: **statically validated**
(`oc kustomize` renders cleanly); live cluster validation pending — if the
verify Job finds no Quay pods, set `QUAY_POD_LABEL` (operator label
conventions vary).

## Validation status

- Source mode (2026-11-03, quay @ `b861d36cf` on
  `fix/projquay-13372-redact-ldap-passwords`): `build && up && verify`
  — 5/5 PASS; Playwright `@auth:OIDC` smoke 19/19 passed.
- Pull mode: not yet run end-to-end.
- OpenShift variant: statically validated only (see above).

## Provenance

Ported from `quay/quay` `.cursor/oidc-testing/` (Apache-2.0); the host-side
`run.sh` was rewritten as stdlib-only `run.py`, and a light data-driven
`verify.py` was added. The Node helpers (`config-proxy.mjs`,
`warmup-oidc.mjs`, `summarize.mjs`), the Keycloak realm, all service
configs and `config/garage/init-garage.sh` (from the quay repo's local-dev)
are ported verbatim. Historical RCA reports and ~1 GB of artifacts stayed
in the quay repo.
