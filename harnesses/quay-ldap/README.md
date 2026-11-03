# quay-ldap — LDAP authentication harness for Quay (PROJQUAY-13372)

Self-contained Docker Compose environment that runs Quay configured for LDAP
authentication against a 389 Directory Server, with `USERS_DEBUG=1` and
`DEBUGLOG=true` — the exact configuration from the PROJQUAY-13372 ticket.
Primary purpose: verify the password-redaction fix (PR #7845) as a
regression gate. Secondary: run the `@auth:LDAP` Playwright specs.

Lean sibling of `../quay-oidc/` (no Clair, Jaeger, Garage, repomirror or
build support).

## Architecture

```
                ┌──────────────────────────────────────────────┐
                │              docker compose network          │
                │                                              │
  host:18090 ───┤─> quay (Quay, LDAP auth)                     │
                │     │        │                               │
                │     │  ldap://quay-ldap:3389                 │
                │     │        └── quay-ldap (389 DS, base.ldif│
                │     │            w/ plaintext + PBKDF2 users)│
                │     ├── quay-db (PostgreSQL 18 + pg_trgm)    │
                │     ├── quay-redis                           │
                │     └── quay-mailpit (SMTP + UI)             │
                │                                              │
                │  playwright runner (@auth:LDAP specs)        │
                └──────────────────────────────────────────────┘
```

The 389 DS is seeded from `ldap/base.ldif` with users whose `userPassword`
values are plaintext (like local-dev) or a fixed PBKDF2 hash (like the
PROJQUAY-13372 ticket log — `uid=hashuser`; this file is the single source
of truth for the hash, which `verify.py` extracts at runtime). With
`USERS_DEBUG=1` the python-ldap trace (trace_level 2) prints every
operation's arguments and results, and `DEBUGLOG=true` raises the Python
root logger so the verify_credentials "DN ... found:" debug line fires. Both
go to the quay container's stdout, which `./run.sh verify` scans for leaks.

## Quick start

```bash
cd harnesses/quay-ldap
cp .env.example .env          # optional; edit to taste

# Pull mode (default): test a published image containing the fix.
# The gate was last validated against: no published tag yet (2026-11-03;
# source mode against the fix branch is the reference run below).
QUAY_IMAGE=quay.io/projectquay/quay:<tag> ./run.sh up
./run.sh verify

# Source mode: build Quay from a local checkout instead.
QUAY_REPO_ROOT=/path/to/quay ./run.sh build
QUAY_REPO_ROOT=/path/to/quay ./run.sh up
./run.sh verify
```

`verify` triggers three LDAP logins through `/api/v1/signin` (valid
credentials, an invalid password, and the PBKDF2-hash user), then scans the
login-window Quay logs for the required redaction shapes and the ticket's
leak patterns. It writes `verify.md`, `quay-since.log` and `quay.log` under
`reports/runs/<timestamp>[-label]/` (`reports/latest` points there) and
exits non-zero on any leak — usable as a regression gate.

The full assertion table lives in `verify.py` (`build_checks()`); add
regression checks by appending entries there.

## Running the Playwright suite

The browser specs live in the quay checkout (`web/playwright/e2e/auth/`), so
`test` requires `QUAY_REPO_ROOT` in either image mode:

```bash
QUAY_REPO_ROOT=/path/to/quay ./run.sh build     # builds the runner image
QUAY_REPO_ROOT=/path/to/quay ./run.sh test      # @auth:LDAP specs
QUAY_REPO_ROOT=/path/to/quay ./run.sh test -- --grep-invert "@container"
```

Rebuild the runner image when the checkout's Playwright pin moves (it is
resolved from `web/package-lock.json` at build time). Specs tagged
`@container`, object-storage and build specs are not runnable here.

## Commands

| Command | What it does |
|---|---|
| `./run.sh build` | Source mode: build quay + runner. Pull mode: pull `QUAY_IMAGE` |
| `./run.sh up` | Start db/redis/mailpit/ldap, then quay; wait for readiness |
| `./run.sh verify` | PROJQUAY-13372 redaction gate (see above) |
| `./run.sh test [args]` | Playwright suite in the runner (default: `@auth:LDAP`) |
| `./run.sh shell` | Bash in the runner container |
| `./run.sh logs [svc]` | Follow compose logs |
| `./run.sh down` / `clean` | Stop / stop + wipe volumes and reports |
| `./run.sh doctor` | Print resolved config; run this first when debugging |
| `./run.sh ocp …` | OpenShift variant (below) |

## Users and credentials

All fixtures are dev-only (see the repo AGENTS.md security note); passwords
are `password` unless noted. Full table in `ldap/base.ldif`.

| User | Password | Notes |
|---|---|---|
| `cn=Directory Manager` | `admin` | LDAP admin bind |
| `testuser` | `password` | regular user |
| `testuser_ldap` / `admin_ldap` | `password` | Playwright LDAP users, `quayMemberOf: cn=test_ldap_group` |
| `readonly_ldap` | `password` | global readonly superuser |
| `hashuser` | (none — PBKDF2 hash) | binds always fail; only the search result matters |

Host-side LDAP check:
`ldapsearch -x -H ldap://localhost:13389 -D 'cn=Directory Manager' -w admin -b dc=example,dc=org`

`clean` (`down -v`) is the only supported way to reset identity state: Quay
caches federated-login state in PostgreSQL, so the DB and LDAP must reset
together.

## OpenShift variant (operator-installed Quay)

`ocp/` deploys the same scenario through the quay-operator: namespace
`quay-ldap-harness`, on-cluster 389 DS (LDIF single-sourced from
`ldap/base.ldif` via a run.py-generated ConfigMap), a `QuayRegistry` with a
config bundle pointing at the in-cluster LDAP service, and a verify Job that
runs **the same `verify.py`** with `--log-source ocp` (logs read through the
Kubernetes API; see `ocp/rbac.yaml` for the least-privilege RBAC).

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
conventions vary), and if the 389 DS pod crash-loops under the restricted
SCC, see the note in `ocp/idp/389ds.yaml`.

## Validation status

- Source mode (2026-11-03, quay @ `b861d36cf` on
  `fix/projquay-13372-redact-ldap-passwords`): `build && up && verify`
  — gate 11/11 PASS; Playwright `@auth:LDAP` 7/7 passed.
- Pull mode: stack boots and verify runs; the redaction gate fails on
  images predating the fix, as it should.
- OpenShift variant: statically validated only (see above).
- Also probed: `DOCKER=podman ./run.sh doctor`.

## Provenance

Ported from `quay/quay` `.cursor/ldap-testing/` (Apache-2.0); the host-side
`run.sh`/`verify.sh` were rewritten as `run.py`/`verify.py` (stdlib-only,
data-driven checks, PBKDF2 literal single-sourced from `base.ldif`).
Historical RCA reports stayed in the quay repo.
