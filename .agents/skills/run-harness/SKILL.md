---
name: run-harness
description: >
  Run a harness that lives in this repository (harnesses/<name>/) — start
  the stack, run the verify gate or Playwright suite, read the reports,
  and tear it down, in compose mode or the OpenShift variant. Use when:
  the user asks to test, verify, or reproduce something against quay (or
  another product) using one of these harnesses, or asks what harnesses
  exist. Not for porting a new harness in (use port-harness).
argument-hint: [harness-name] [command]
allowed-tools:
  - Bash(ls harnesses/*)
  - Bash(cd harnesses/* && ./run.sh *)
  - Bash(cd harnesses/* && QUAY_REPO_ROOT=* ./run.sh *)
  - Bash(cat *)
  - Bash(tail *)
  - Bash(grep *)
  - Read
  - Grep
  - AskUserQuestion
---

# Run a Testing Harness

Run `$ARGUMENTS` (harness name and/or command). If no harness is named,
list what exists: `ls harnesses/`, then read the harness's README.md.

## Step 1 — Doctor first, always

```bash
cd harnesses/<name> && ./run.sh doctor
```

This prints the resolved mode (pull vs source), images, docker user,
compose files. Do not run anything else until doctor's output matches the
intended mode. Common mismatch: `PRODUCT_REPO_ROOT` set from a previous
command in the same shell — env wins over `.env` by design.

## Step 2 — Pick the mode

- **Pull mode (default)**: tests a published image, `QUAY_IMAGE`
  (default per harness). No product checkout needed for `up`/`verify`.
- **Source mode**: set `QUAY_REPO_ROOT=/path/to/product/checkout` —
  run.py appends compose.build.yaml and builds instead of pulling. Use
  this to test unmerged fixes; the quay-ldap redaction gate is only
  green in this mode until a fixed image is published.
- `test` (the Playwright suite) needs `QUAY_REPO_ROOT` in BOTH modes —
  the suite bind-mounts from the checkout.

## Step 3 — Lifecycle

Compose mode (details: `references/compose-lifecycle.md`):

```bash
./run.sh build && ./run.sh up && ./run.sh verify    # gate + stack
./run.sh test -- --grep "@auth:LDAP"                # suite slice
./run.sh down                                       # keep volumes
./run.sh clean                                      # wipe volumes too — read
                                                    # the harness README first
```

OpenShift variant (details: `references/ocp-lifecycle.md`): operator must
be installed; `./run.sh ocp apply && ./run.sh ocp verify && ./run.sh ocp delete`.

## Step 4 — Read the results

Everything lands under `reports/latest/` (symlink to
`reports/runs/<timestamp>[-<label>/`):

- `verify.md` — the gate table. FAIL rows with `required` semantics:
  hard failures exit non-zero; warnings do not.
- `playwright-report/index.html` + `test-results/` — suite results,
  traces, videos.
- `logs/` — suite stdout and the product service logs captured at run
  time. When reporting a failure, cite the report directory path and the
  image tag/mode from doctor, not paraphrases.

## Step 5 — Troubleshooting

Before debugging anything, run `references/troubleshooting.md` against
the symptom. The frequent ones: rootless docker `user:` errors (doctor
shows the resolved DOCKER_USER), OIDC identity poisoning after volume
resets (quay-oidc README, "Keycloak identity stability"), image-vs-suite
skew in pull mode, and the compose project name being changed (breaks
named-volume reuse).

## Conventions while working

- Never commit anything under `reports/`, `bin/`, `ocp/generated/`, or a
  `.env` — all gitignored on purpose.
- All credentials in these harnesses are intentional dev-only fixtures;
  never replace them with real secrets.
- Leave the stack running only if the user wants it; otherwise `down`
  (preserves state) rather than `clean` (wipes it) unless asked.
