# Validation levels

Validate at every level you can, and record honestly in the harness
README which ones ran. "Statically validated only" is an acceptable final
state when no live environment is available; claiming more than ran is not.

## Level 0 — static (always, before any commit)

```bash
cd harnesses/<name>
python3 -m py_compile run.py verify.py
./run.sh doctor
docker compose -f compose.yaml --profile test config -q          # pull mode
QUAY_REPO_ROOT=/path/to/product docker compose -f compose.yaml \
  -f compose.build.yaml --profile test config -q                  # source mode
./run.sh ocp render > /dev/null && echo OK                        # or: oc kustomize ocp/
```

Check the rendered source-mode compose actually contains the
`QUAY_BUILT_IMAGE` tag on every service that shares the product image,
and that the runner's `additional_contexts` point at the repo root.

If bash was ported: `shellcheck` (or careful review when unavailable).

## Level 1 — live stack, source mode

Requires the product source checkout (ideally one containing whatever
fix/feature the harness gates — the point is proving the gate detects it):

```bash
QUAY_REPO_ROOT=/path/to/product ./run.sh build
QUAY_REPO_ROOT=/path/to/product ./run.sh up
./run.sh verify
cat reports/latest/verify.md
```

Success = every required check PASS. If the gate FAILs while the fix is
present, the port broke the choreography — compare against the source
harness's messages and waits before touching check logic.

Also run `up`/`verify` once with the repo root UNSET (pull mode) to prove
the stack is checkout-independent; note that a pull-mode gate run needs a
published image containing the fix, otherwise failure is the *correct*
outcome — record which image tag the gate was last validated against.

## Level 2 — test suite

```bash
QUAY_REPO_ROOT=/path/to/product RUN_LABEL=smoke \
  ./run.sh test -- --grep "<narrow tag>"
```

Run a narrow slice first (auth tag, critical tag), not the full suite.
Check `reports/latest/`: the HTML report, the raw results, and the log
dumps. A suite failure with a green verify gate usually means runner-image
problems (wrong Playwright pin — rebuild the runner when the checkout's
lockfile moves) or fixture drift.

## Level 3 — OpenShift

Only with a real cluster and the operator installed via OLM:

```bash
./run.sh ocp apply    # then verify
./run.sh ocp verify
./run.sh ocp delete
```

Expected unknowns on first live run (both env-overridable): the product
pod label convention (`<PRODUCT>_POD_LABEL`) and SCC restrictions on
older base images (389ds needs anyuid on OpenShift).

## Reporting

In the harness README, state the validation status like:

> Source mode: build + up + verify gate 11/11 PASS and Playwright
> @auth:LDAP 7/7 passed against quay @ b861d36cf (2026-11-03).
> Pull mode: stack boots; the redaction gate requires an image containing
> the fix (validated against none yet). OCP: statically validated only.

In PR/commit messages, report each command and its outcome — passed,
failed, not run, or infrastructure-blocked — the same way the quay repo's
AGENTS.md requires.
