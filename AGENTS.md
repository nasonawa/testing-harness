# AGENTS.md

AI-optimized guide for working in this repository — a personal "harness
dictionary" of self-contained testing harnesses for Quay (and, over time,
other products), runnable by humans and by coding agents (Claude Code,
Codex, Cursor, ...).

## What lives here

Each directory under `harnesses/` is one **self-contained, copyable
harness**: a compose stack, an orchestrator, a verify gate, and an
OpenShift variant for one product + one test scenario.

| Harness | Proves |
|---|---|
| `harnesses/quay-ldap/` | LDAP auth works and user passwords stay redacted in Quay's logs (PROJQUAY-13372) |
| `harnesses/quay-oidc/` | OIDC auth against Keycloak end-to-end (browser suite, full CI-parity stack) |
| `harnesses/_template/` | Skeleton for new harnesses — start here, not from a sibling |

## Structure contract (mandatory)

```
harnesses/<product>-<mode>/
├── compose.yaml          pull-mode; NO build: key on the product service
├── compose.build.yaml    source-mode override (run.py appends it)
├── Containerfile.playwright   runner image; product-repo files via named context
├── run.py / run.sh       orchestrator (stdlib Python 3) + shim
├── verify.py             data-driven verify gate (stdlib Python 3)
├── .env.example          every DEFAULTS key, documented
├── README.md             quickstart, commands, credentials, provenance, validation status
├── config/ init/ fixtures/   stack configs and seed data
└── ocp/                  OpenShift variant (kustomize; generated/ is gitignored)
```

Generated/local state — **never commit**: `.env`, `reports/`, `bin/`,
`ocp/generated/`, `compose.override.yaml`, `local-dev/` (all gitignored).

## The conventions (follow them exactly)

1. **Image source is env-switchable, pull by default.** The product
   service in `compose.yaml` pulls `${<PRODUCT>_IMAGE:-<default>}`.
   Setting `<PRODUCT>_REPO_ROOT` makes run.py append
   `compose.build.yaml` (build + retag to `<PRODUCT>_BUILT_IMAGE` — a
   *different* tag, so local builds never shadow the published ref).
   Every service sharing the product image must be overridden together.
2. **`up`/`verify` never need the product source checkout**; only `test`
   does (the suite bind-mounts from it).
3. **Host-side Python is stdlib-only** — run.py and verify.py must run on
   any python3, including inside the OCP verify Job (`python:3.12-slim`,
   zero pip). In-container helpers stay in whatever language the source
   harness used.
4. **Config layering**: `DEFAULTS` ← `.env` ← environment ← CLI. Never
   read env vars outside `load_config()`.
5. **Single-source shared literals**: a value used by both a fixture and
   a check is extracted at runtime from one file, and the extraction
   raises loudly on drift.
6. **Copyability over DRY**: no imports between harnesses. When a
   mechanism improves, apply it to every harness *and* `_template/`.
7. **`doctor` first, always** — side-effect-free, prints resolved mode,
   images, DOCKER_USER, compose files. Include its output in bug reports.
8. **Reports**: `reports/runs/<YYYYMMDD-HHMMSS>[-<label>]/` +
   `reports/latest` symlink; verify.md is the gate result.

## Extension points (the intended ways to customize)

- **New assertions**: append `Check(...)` entries to `build_checks()` in
  verify.py — that table is the whole gate.
- **Stack changes**: edit `config/` (product + dependency configs) or
  `init/` (first-boot SQL/scripts).
- **Compose-level tweaks without editing tracked files**: drop a
  `compose.override.yaml` next to compose.yaml — run.py auto-includes it.
- **Env knobs**: everything in `.env.example` (ports, images, labels,
  namespaces, DOCKER/DOCKER_USER, RUN_LABEL).
- **OCP additions**: add resources to `ocp/` kustomization; fixture data
  the stack needs at apply time is a tracked ConfigMap under `ocp/`
  (refreshed by run.py from the fixture, so a bare `oc apply -k ocp/`
  works from a fresh clone); run.py-generated ConfigMaps in
  `ocp/generated/` are for per-run artifacts only (verify code, resolved
  URLs) — kustomize cannot read above `ocp/`.
- **New harness**: copy `harnesses/_template/` and resolve every
  EDIT-ME, or use the `port-harness` skill for an existing ad-hoc harness.

## Reproducibility expectations

- A harness run is *defined* by: harness commit + mode (pull/source) +
  image tag(s) + `.env`/overrides. State all of them when reporting.
- Run `./run.sh doctor`, then the gate (`verify` or `test`), and cite the
  `reports/runs/<...>/` directory (not paraphrases) as evidence.
- Each harness README carries a **validation status** section listing
  which levels actually ran and when — keep it current when you re-run;
  never claim validation that did not happen.

## Security note (read this once)

Every credential in every harness — Keycloak admin/admin, the LDAP
Directory Manager password, Garage S3 keys, PBKDF2 fixture hashes, dummy
client secrets — is an **intentional dev-only fixture** committed on
purpose so the harnesses are reproducible. They carry `# gitleaks:allow`
style provenance where relevant. **Never** replace them with real
secrets, never reuse them anywhere real, and never add real credentials
to this repo.

## Skills

- `/port-harness /path/to/source` — port an ad-hoc harness into this repo
  (disposition, decoupling audit, template instantiation, validation).
- `/run-harness [name] [command]` — run an existing harness: doctor,
  lifecycle, reports, troubleshooting.

Both live in `.agents/skills/` (`.claude/skills` symlinks there).

## Provenance

Both quay harnesses were ported from the quay/quay repo's untracked
`.cursor/oidc-testing/` and `.cursor/ldap-testing/` (working tree of
branch `fix/projquay-13372-redact-ldap-passwords` @ `b861d36cf`,
2026-11-03); `init-garage.sh` came from the tracked file last touched by
`2619dcc160`. Everything here is Apache-2.0, matching quay/quay. Each
harness README documents its own verbatim/adapted/rewritten/dropped
dispositions in detail.
