# Porting checklist

Print this and tick items off; the skill workflow references it.

## Source analysis

- [ ] Full file inventory recorded (`find <src> -type f | sort`)
- [ ] What the harness proves stated in one sentence
- [ ] Every file dispositioned: verbatim / adapted / rewrite / drop
- [ ] Dropped artifacts listed in the README provenance section with where
      they still live
- [ ] Stable identifiers identified (compose project name, volume names,
      host ports, pinned UUIDs) and preserved
- [ ] Dev-only fixture credentials identified (they are committed on
      purpose — flag them for the README credentials section and the repo
      AGENTS.md security note)

## Decoupling (see decoupling-patterns.md)

- [ ] No `build:` key on the product service in compose.yaml
- [ ] compose.build.yaml overrides every service sharing the product image
- [ ] Built image tag differs from the published ref
- [ ] No `../../` anywhere in compose or scripts
- [ ] Containerfile uses named context for product-repo files
- [ ] `up`/`verify` work with no product checkout present
- [ ] Shared literals single-sourced with loud runtime extraction

## Harness completeness

- [ ] `grep -rn "EDIT-ME"` returns nothing
- [ ] run.py commands cover the source run.sh's surface
- [ ] verify.py CHECKS table implements the stated proof
- [ ] `.env.example` covers every DEFAULTS key
- [ ] README follows the TEMPLATE-README skeleton (quickstart, commands,
      reports, credentials, OCP, provenance, validation status)

## OCP variant (if in scope)

- [ ] Namespace + least-privilege RBAC (pods get/list, pods/log get only)
- [ ] Config bundle strips operator-managed keys
- [ ] Fixture data reaches the cluster via generated ConfigMaps, not
      duplicated YAML
- [ ] verify Job mounts the same verify.py (`--log-source ocp`)
- [ ] `ocp verify` re-applies generated ConfigMaps before the Job
- [ ] Job template metadata name is unique enough that run.py's name
      substitution cannot collide with other references

## Validation (see validation.md)

- [ ] `python3 -m py_compile run.py verify.py`
- [ ] `./run.sh doctor`
- [ ] `docker compose -f compose.yaml --profile test config -q` (pull mode)
- [ ] same with `compose.build.yaml` + `<PRODUCT>_REPO_ROOT` set (source)
- [ ] `./run.sh ocp render` (or `oc kustomize ocp/`)
- [ ] live e2e `build && up && verify` if the product source is available
- [ ] test-suite slice if the harness ships one
- [ ] README states which validation levels actually ran
