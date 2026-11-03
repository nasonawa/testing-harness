---
name: port-harness
description: >
  Port an existing ad-hoc testing harness (a directory of compose files,
  run scripts, fixtures and configs, usually living inside a product repo)
  into this repository as a self-contained generic harness under
  harnesses/<product>-<mode>/. Use when: the user asks to port, move, or
  genericize a testing harness into the testing-harness repo, or to add a
  new harness for a product that already has ad-hoc test scripts elsewhere.
  Not for merely running an existing harness (use run-harness) or for
  creating a harness from scratch with no source (start from
  harnesses/_template/ directly).
argument-hint: /path/to/source-harness
allowed-tools:
  - Bash(ls *)
  - Bash(cat *)
  - Bash(grep *)
  - Bash(find *)
  - Bash(cp *)
  - Bash(mkdir *)
  - Bash(diff *)
  - Bash(python3 *)
  - Bash(./run.sh *)
  - Bash(cd harnesses/* && ./run.sh *)
  - Read
  - Write
  - Edit
  - AskUserQuestion
---

# Port a Testing Harness

Port the source harness at `$ARGUMENTS` into this repo following the plan
below. Read `harnesses/_template/TEMPLATE.md` first — it is the contract
every harness in this repo implements. The finished quay-ldap and
quay-oidc harnesses are the reference implementations; when in doubt,
match them.

## Stage A — Inventory and disposition

1. Walk the source tree: `find $ARGUMENTS -type f | sort`. Classify every
   file into exactly one bucket and record the disposition in the harness
   README's provenance section:
   - **verbatim** — container-internal scripts, fixtures, configs with no
     product-repo path assumptions. Copy unchanged.
   - **adapted** — needs decoupling changes (Stage B). Copy, then edit.
   - **Python rewrite** — host-side orchestration/verification shell
     scripts. Rewrite against the `_template/` run.py/verify.py skeletons,
     keeping the *choreography* literal (order, waits, retries, messages).
   - **drop** — one-off artifacts: RCA reports, gigabyte logs/binaries,
     anything that will rot. The README links back to them in the source
     repo instead.
2. Identify what the harness *proves* (a regression gate? an auth flow?).
   That becomes the verify.py CHECKS table — if you cannot state it, ask
   the user before continuing.

## Stage B — Coupling audit

Grep the source for every coupling class and resolve each (details and
fixes in `references/decoupling-patterns.md`):

```bash
grep -rn "\.\./\.\.\|REPO_ROOT\|build:" $ARGUMENTS --include="*.yaml" --include="*.sh"
```

- [ ] `build.context: ../..` → pull-default `image:` in compose.yaml +
      `compose.build.yaml` override (retag to a *different* tag)
- [ ] `REPO_ROOT=...` path derivation → `<PRODUCT>_REPO_ROOT` env +
      `load_config()` layering
- [ ] `../../` bind mounts → `${<PRODUCT>_REPO_ROOT:-.}/...`
- [ ] Containerfile `COPY ../../...` → BuildKit named context
      (`additional_contexts`, `COPY --from=<product>repo`)
- [ ] suite coupling: does `up`/`verify` work without the product source
      checkout? They must; only `test` may require it.
- [ ] shared literals duplicated between fixtures and checks → single
      source + runtime extraction that raises on drift

## Stage C — Instantiate the template

```bash
cp -r harnesses/_template harnesses/<product>-<mode>
grep -rn "EDIT-ME" harnesses/<product>-<mode>   # work until this is empty
```

Rename the `PRODUCT_*` env keys to the product's prefix. Keep the stable
identifiers from the source (compose project name, volume names, host
ports, user UUIDs) — state in the source sometimes depends on them
(quay-oidc's `keycloak-data` volume is the load-bearing example).

## Stage D — run.py / verify.py port

- Port the run.sh choreography into the template's `cmd_*` functions,
  command for command. Add product-specific subcommands where the source
  had them (fetch/report/...). Do not "improve" ordering or waits.
- Build the CHECKS table in verify.py: hard preconditions first and
  `required=True`; soft checks `required=False` (warnings). Keep
  `--log-source compose|ocp|file:PATH` working — the OCP Job reuses this
  file verbatim.
- Add an `.env.example` documenting every DEFAULTS key.

## Stage E — OpenShift variant

Port `ocp/` from quay-ldap (closest reference): namespace, least-privilege
RBAC, on-cluster dependencies, config bundle minus operator-managed keys,
operator CR, verify Job running the same verify.py with `--log-source
ocp`. Generated ConfigMaps live in gitignored `ocp/generated/`, written by
`write_generated_manifests()` from the harness's single-source files —
kustomize cannot read above `ocp/`, which is why the indirection exists.

## Stage F — Validate

Follow `references/validation.md` exactly. The minimum bar before
committing: py_compile, doctor, compose render in both modes, kustomize
render, and (when the product source is available) a real `build && up &&
verify` with the gate green. Report honestly which levels passed — a
statically-validated-only harness is acceptable if labeled as such in its
README.

## Commit

One commit per harness: `feat(<product>-<mode>): port <thing> harness from
<repo/path>`, listing in the body what was verbatim/adapted/rewritten/
dropped and the validation results. End with the repo's attribution line.
