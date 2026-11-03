# Harness template

Distilled from `harnesses/quay-ldap`. Copy this directory to
`harnesses/<product>-<mode>/` and resolve every `EDIT-ME` marker (grep for
it — the harness is not done until `grep -r "EDIT-ME" .` is empty). The
quay-ldap and quay-oidc harnesses are the reference implementations; when
this template and they disagree, they win.

```
<product>-<mode>/
├── README.md               ← rewrite from TEMPLATE-README below
├── compose.yaml            pull-mode compose (image: ${<PRODUCT>_IMAGE:-...})
├── compose.build.yaml      source-mode override (build: from <PRODUCT>_REPO_ROOT)
├── Containerfile.playwright  runner image (COPY --from=<product>repo for repo files)
├── run.py                  host-side orchestrator (stdlib-only)
├── run.sh                  thin shim — copy verbatim, no edits needed
├── verify.py               data-driven verify gate (stdlib-only)
├── .env.example            every env var with a comment
├── config/                 product + dependency configs
├── init/                   first-boot SQL/scripts for stateful deps
├── fixtures/               (rename freely) ldif / realm json / seed data
└── ocp/                    OpenShift variant (see below)
```

## Marker index

| Marker | Where | What goes there |
|---|---|---|
| `EDIT-ME product` | everywhere | product name, used in service/volume/namespace/image names |
| `EDIT-ME services` | `compose.yaml` | the product service + its dependencies |
| `EDIT-ME checks` | `verify.py` `build_checks()` | the assertions that define "this harness proves X" |
| `EDIT-ME choreography` | `run.py` `cmd_up()` | startup order + readiness waits |
| `EDIT-ME generated` | `run.py` `write_generated_manifests()` | ocp/ ConfigMaps to materialize from single sources |
| `EDIT-ME bundle` | `ocp/config-bundle.yaml` | product config minus operator-managed keys |

## Porting rules (the contract)

1. **Image source is env-switchable, pull by default.** `compose.yaml`
   never has a `build:` key for the product service — it pulls
   `${<PRODUCT>_IMAGE:-<default ref>}`. `compose.build.yaml` adds the
   `build:` and retags to `<PRODUCT>_BUILT_IMAGE` (a *different* tag, so a
   local build can never shadow the published ref). `run.py` appends the
   build file iff `<PRODUCT>_REPO_ROOT` is set. If more than one service
   shares the product image (e.g. quay + repomirror), the build override
   must switch **all** of them together.
2. **Playwright runner is profile-gated** (`profiles: ["test"]`) and built
   with a BuildKit named context: `additional_contexts:
   {<product>repo: ${<PRODUCT>_REPO_ROOT:-.}}`. The Containerfile
   `COPY --from=<product>repo ...` for product-repo files and plain `COPY`
   for harness-local files. The test suite bind-mounts from
   `${<PRODUCT>_REPO_ROOT}/...` — `test` requires the checkout even in
   pull mode; `up`/`verify` must not.
3. **Host-side Python is stdlib-only** (`run.py`, `verify.py`) so it runs
   anywhere python3 exists, including inside a slim OCP verify Job.
   In-container helpers stay in whatever language the source harness used.
4. **Config layering**: `DEFAULTS` ← `.env` ← environment ← CLI. Never
   read env directly outside `load_config()`.
5. **Single-source shared literals.** If a value appears in both a fixture
   and a check (a hash, a secret, an issuer URL), one file is the source
   of truth and the other extracts it at runtime and raises loudly on
   drift.
6. **Copyability over DRY.** No imports between harnesses. When improving
   a mechanism, fix all harnesses + this template.
7. **Reports** go to `reports/runs/<YYYYMMDD-HHMMSS>[-<label>]/` with a
   `reports/latest` symlink; `reports/` is gitignored. Every destructive
   or stateful command prints where its artifacts landed.
8. **`doctor` is the first command.** It prints resolved mode, images,
   DOCKER_USER, socket, repo-root presence. Users and agents run it before
   anything else; keep it cheap and side-effect-free.

## OpenShift variant

`ocp/` mirrors the compose scenario through the platform's operator (for
quay: `QuayRegistry`). The verify Job runs **the same `verify.py`** with
`--log-source ocp` — that reuse seam is the point of the `LogSource`
classes in verify.py. Kustomize reads ConfigMaps from `ocp/generated/`,
which is gitignored and materialized by `run.py ocp render|apply` from the
harness's single-source files (kustomize's load restrictor forbids paths
above `ocp/`). Tell users to render/apply **only** through `./run.sh ocp`.

Least-privilege RBAC for the verify Job: ServiceAccount + namespaced Role
(`get,list` pods; `get` pods/log) + RoleBinding in `ocp/rbac.yaml` — no
cluster-scoped anything.

## TEMPLATE-README (skeleton for the harness README)

```markdown
# <name> — <one line: what this harness proves>

Architecture: <ascii diagram of services and ports>

## Quick start
    cp .env.example .env
    <PRODUCT>_IMAGE=<ref> ./run.sh up          # pull mode
    <PRODUCT>_REPO_ROOT=/path/to/src ./run.sh build && ./run.sh up   # source mode

## Commands      (table: build/up/verify/test/shell/logs/down/clean/doctor/ocp)
## Reports       (reports/latest layout, what to attach to a bug)
## Credentials   (dev-only fixtures table + link to repo AGENTS.md security note)
## OpenShift     (operator prereq, ocp apply/verify/logs/delete, validation status)
## Provenance    (ported from <repo/path> @ <commit>, what changed)
```

State the validation status honestly: which modes were run end-to-end and
which are "statically validated only".
