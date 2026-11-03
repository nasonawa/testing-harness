# Decoupling patterns

The six coupling classes found in product-repo-resident harnesses, and the
fix for each. All examples use quay; substitute the product prefix.

## 1. Build context pointing at the product repo

Source:

```yaml
services:
  quay:
    build:
      context: ../..          # the product repo root
      dockerfile: Dockerfile
```

Fix — split by image source, pull by default:

```yaml
# compose.yaml (pull mode, the default)
services:
  quay:
    image: ${QUAY_IMAGE:-quay.io/projectquay/quay:latest}   # no build: key

# compose.build.yaml (run.py appends this iff QUAY_REPO_ROOT is set)
services:
  quay:
    build:
      context: ${QUAY_REPO_ROOT}
      dockerfile: Dockerfile
      target: final
    image: ${QUAY_BUILT_IMAGE:-localhost/<harness>-quay:latest}
```

Rules:
- The built tag MUST differ from the published ref — otherwise a local
  build shadows the published image and later pull-mode runs silently test
  stale code.
- If sibling services share the image (quay + repomirror), the override
  must switch ALL of them together. A missed sibling silently pulls the
  published image: you build a fix, the worker under test never sees it.
- run.py detects source mode as `bool(cfg["QUAY_REPO_ROOT"])` — nothing
  else, so `.env`, environment and CLI all work through the same seam.

## 2. REPO_ROOT path derivation in scripts

Source:

```bash
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
```

Fix: `<PRODUCT>_REPO_ROOT` env var resolved through `load_config()`
(DEFAULTS ← .env ← environment ← CLI). In compose files it appears as
`${QUAY_REPO_ROOT:-.}` so interpolation never fails when unset.

## 3. Bind mounts reaching above the harness

Source: `- ../../web/playwright:/work/playwright:Z`

Fix: `- ${QUAY_REPO_ROOT:-.}/web/playwright:/work/playwright:Z`. The `:-.`
default keeps `docker compose config` renderable without a checkout; the
mount is only *used* by `test`, which fails fast (check_repo_root) when
the checkout is missing.

## 4. Containerfile COPYs from the product repo

A build context is a single directory, so the runner Containerfile cannot
see both the harness dir and the product repo. Fix — BuildKit named
additional contexts:

```yaml
playwright:
  profiles: ["test"]
  build:
    context: .                          # the harness dir
    dockerfile: Containerfile.playwright
    additional_contexts:
      quayrepo: ${QUAY_REPO_ROOT:-.}    # the product repo
```

```dockerfile
COPY --from=quayrepo web/package-lock.json /tmp/...   # product-repo file
COPY entrypoint.sh /usr/local/bin/entrypoint.sh        # harness-local file
```

The `${QUAY_REPO_ROOT:-.}` default means a runner built in pull mode has a
dummy (empty) quayrepo context — fine, because in pull mode only
harness-local COPY layers are needed... if the runner needs product files
unconditionally, make `test` require the repo root (it usually does anyway
for the bind-mounted suite).

## 5. Suite coupling

The test suite bind-mounts from the checkout, so `test` needs it even in
pull mode — that is expected. The invariant to preserve: **`up` and
`verify` must not need the product source**. Anything the stack itself
needs must be copied into the harness (config/, init/, fixtures/).

Verify this explicitly: run `up` + `verify` with `<PRODUCT>_REPO_ROOT`
unset before declaring the port done.

## 6. Duplicated literals between fixtures and checks

If a fixture secret/hash appears in both a fixture file and a check
script, the two copies drift and the check silently rots. Fix — one file
is the source of truth; the other extracts at runtime and fails loudly:

```python
def ldif_userpassword(ldif_path, uid):
    text = ldif_path.read_text()
    m = re.search(rf"dn: uid={re.escape(uid)},.*?^userPassword: (\S+)",
                  text, re.S | re.M)
    if not m:
        raise RuntimeError(f"userPassword for {uid} not found in {ldif_path} — "
                           "fixture format drifted; update the extractor")
    return m.group(1)
```

The same principle applies to issuer URLs, ports and user names that
appear in more than one place.
