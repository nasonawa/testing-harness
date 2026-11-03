# testing-harness

A personal **harness dictionary**: self-contained, agent-runnable testing
harnesses for Quay today and other products later. Each harness is one
directory you can copy, `./run.sh up`, and point a bug report at.

Design goals: runnable by humans *and* coding agents (Claude Code, Codex,
Cursor), pull-a-published-image by default with build-from-source as an
env switch, and a verify gate that states in one table whether the thing
under test actually works. See [AGENTS.md](AGENTS.md) for the full
conventions.

## Harnesses

| Harness | Product | Exercises | Compose | OpenShift |
|---|---|---|---|---|
| [quay-ldap](harnesses/quay-ldap/) | Quay | LDAP auth + password redaction in logs (PROJQUAY-13372) | ✅ | manifests (statically validated) |
| [quay-oidc](harnesses/quay-oidc/) | Quay | OIDC login against Keycloak, full CI-parity stack | ✅ | manifests (statically validated) |
| [_template](harnesses/_template/) | any | skeleton for the next port | — | — |

✅ = validated end-to-end; see each harness README's "Validation status".

## Quick start (any harness)

```bash
cd harnesses/quay-ldap          # or quay-oidc
./run.sh doctor                 # always first: resolved mode/images/user

# Pull mode (default) — test a published image:
QUAY_IMAGE=quay.io/projectquay/quay:<tag> ./run.sh up
./run.sh verify

# Source mode — build the product from a local checkout:
QUAY_REPO_ROOT=/path/to/product ./run.sh build
QUAY_REPO_ROOT=/path/to/product ./run.sh up
./run.sh verify
```

Results land in `reports/runs/<timestamp>[-<label>]/` (`reports/latest`
symlinks to the newest): `verify.md` for the gate, `playwright-report/`
and `test-results/` for the suite, `logs/` for captured service output.

Running the browser suite additionally needs the product checkout
(`QUAY_REPO_ROOT`) in both modes — the suite bind-mounts from it:

```bash
QUAY_REPO_ROOT=/path/to/product ./run.sh test -- --grep "@auth:LDAP"
```

## Commands

Every harness exposes the same surface (details per harness README):

`build` · `up` · `verify` · `test [args]` · `shell` · `logs [svc]` ·
`down` · `clean` · `doctor` · `ocp <render|apply|verify|logs|delete>`

## Environment variables

Every harness follows the same pattern (rename `PRODUCT` to the product
prefix); `.env.example` in each harness is the authoritative list.

| Variable | Meaning | Default |
|---|---|---|
| `DOCKER` | container engine: docker / podman | `docker` |
| `PRODUCT_IMAGE` | pull-mode image ref | per harness |
| `PRODUCT_REPO_ROOT` | product source checkout — **set = source mode** | unset |
| `PRODUCT_BUILT_IMAGE` | tag for locally built images (deliberately ≠ `PRODUCT_IMAGE`) | per harness |
| `PRODUCT_HOST_PORT` | host port for the product UI/API | per harness |
| `DOCKER_USER` | runner container user; empty = auto-detect (rootless Docker → `0:0`) | auto |
| `RUN_LABEL` | suffix for the report directory name | empty |
| `PRODUCT_POD_LABEL` | (OCP) label selecting product pods | per harness |
| `OCP_NAMESPACE` | (OCP) target namespace | per harness |

## OpenShift variant

Each quay harness ships an `ocp/` kustomize set that deploys the same
scenario through the quay-operator: on-cluster IdP, `QuayRegistry` with a
config bundle, and a verify Job running **the same verify.py** with
`--log-source ocp` (least-privilege RBAC: pods get/list + pods/log get).
The browser suite is compose-only.

Prerequisites: quay-operator installed via OLM, `oc` logged in.

```bash
./run.sh ocp apply     # ~10-15 min; waits for the QuayRegistry
./run.sh ocp verify
./run.sh ocp delete
```

Render/apply **only** through `./run.sh ocp` — the kustomization
references gitignored ConfigMaps that run.py generates from the harness's
single-source files.

## For AI agents

Read [AGENTS.md](AGENTS.md) before doing anything else. Two skills are
installed (`.agents/skills/`, symlinked from `.claude/skills/`):

- **port-harness** — port an ad-hoc harness from a product repo into this
  one (inventory → decoupling audit → template → validation).
- **run-harness** — run an existing harness (doctor → lifecycle →
  reports → troubleshooting).

## Adding a harness

Copy `harnesses/_template/` and resolve every `EDIT-ME` marker
(`grep -rn EDIT-ME` until empty); [TEMPLATE.md](harnesses/_template/TEMPLATE.md)
walks through the contract. For porting an existing ad-hoc harness, use
the port-harness skill.

## Security

All credentials in these harnesses are intentional dev-only fixtures
committed for reproducibility (Keycloak admin/admin, LDAP Directory
Manager, Garage keys, PBKDF2 hashes, dummy client secrets). Never replace
them with real secrets and never reuse them anywhere real.

## License

Apache-2.0, inherited from [quay/quay](https://github.com/quay/quay),
from which the first two harnesses were ported (2026-11-03, branch
`fix/projquay-13372-redact-ldap-passwords` @ `b861d36cf`; the source
harnesses were untracked working-tree directories under `.cursor/`).
Per-harness dispositions: see each harness README's provenance section.
