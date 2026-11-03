# OCP lifecycle reference

The OpenShift variant deploys the same scenario through the platform's
operator (quay: `QuayRegistry` from operators.coreos.com) and runs **the
same verify.py** in a Job with `--log-source ocp`. Browser suites are
compose-only — no Routes are exposed.

## Prerequisites

- `oc` logged in with permission to create namespaces in the target
  cluster/project.
- The product's operator installed via OLM in the target cluster
  (Quay: `Red Hat Quay` / `quay-operator` in OperatorHub).
- Check with `oc get quayregistries -A` (quay) that the CRD exists.

## Commands

All go through `./run.sh ocp <subcommand>` — never raw `oc apply -k`,
because the kustomization references `ocp/generated/` ConfigMaps that
only run.py materializes (gitignored; kustomize's load restrictor forbids
reading files above `ocp/`, which is why the indirection exists).

- `render` — regenerate + print the full manifest set. Safe, read-only.
  12 resources for each quay harness: namespace, 2 SAs, role + binding,
  3 generated ConfigMaps, config-bundle Secret, IdP service + deployment,
  and the operator CR.
- `apply` — deploy everything, then poll the operator CR's status until
  the registry is Available (bounded wait, ~10–15 min for Quay). If the
  wait fails, `ocp logs` + `oc describe quayregistry <name> -n <ns>`.
- `verify` — resolve the external URL from the CR status, re-apply the
  generated ConfigMaps (the params ConfigMap carries the resolved URL),
  then apply the verify Job with a unique name and follow it to
  completion. verify.md is extracted from the Job stdout (between the
  BEGIN/END VERIFY.MD markers) into `reports/runs/<ts>-ocp/`.
- `logs` — follow the product deployment's logs.
- `delete` — remove everything in the harness namespace.

## Verify Job mechanics (why it is shaped this way)

- Image `python:3.12-slim`, zero pip — verify.py is stdlib-only by
  contract precisely so this Job needs nothing else.
- Logs are read through the Kubernetes API using the Job's own
  ServiceAccount (token + CA at /var/run/secrets/kubernetes.io/), least
  privilege: pods get/list + pods/log get in the harness namespace only.
- `backoffLimit: 0`: a failed check surfaces once, not retried into a
  confusing transcript.
- The Job template's metadata name is distinctive
  (`<harness>-verify-job`) so run.py's per-run name substitution cannot
  collide with the configMap volume reference.

## First live run — expected friction

- **No pods found by the log source**: operator label conventions vary;
  override `<PRODUCT>_POD_LABEL` (default `app=quay`).
- **IdP CrashLoop**: older images (389ds) expect to run as a fixed uid
  and need the anyuid SCC on OpenShift — noted in `ocp/idp/389ds.yaml`.
- **Registry never becomes Available**: `oc describe` the CR; the usual
  cause is a config-bundle key the operator manages (storage/db/redis)
  that was not stripped — compare against `ocp/config-bundle.yaml`.
