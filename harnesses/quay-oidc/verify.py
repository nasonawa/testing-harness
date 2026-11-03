#!/usr/bin/env python3
"""Light verification for the OIDC harness (data-driven, stdlib only).

The OIDC harness's real verification is the Playwright suite (`./run.sh
test`). This gate covers the API-only scenarios — compose smoke checks and
the OpenShift verify Job — where no browser is available:

  * Quay's /health/instance answers 200
  * Keycloak's OIDC discovery document is served and its `issuer` matches
    the issuer Quay is configured with (a mismatch breaks every login)
  * no Keycloak realm client secret appears in Quay's logs

Deliberately NO password sign-ins: Quay binds federated identities on first
login, so a password signin for an OIDC user would poison the `sub` binding
(see warmup-oidc.mjs and the RCA history in the quay repo).

Runs in two environments (the --log-source seam):
  * compose: on the host, `docker compose logs quay` (default)
  * ocp:     inside the OpenShift verify Job, via the Kubernetes API using
             the pod's service-account token (needs pods + pods/log get)
  * file:PATH: replay a previously captured log

Extension point: add checks by appending entries to CHECKS in
build_checks() below. For deeper OIDC coverage (token exchange, audience
validation), enable password grant on a dedicated realm client first.

Usage:
  verify.py [--base-url URL] [--keycloak-url URL] [--keycloak-issuer ISS]
            [--log-source compose|ocp|file:PATH] [--report-dir DIR]
            [--print-report]
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import http.cookiejar
import json
import os
import pathlib
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HARNESS_DIR = pathlib.Path(__file__).resolve().parent

VERIFY_BEGIN = "-----BEGIN VERIFY.MD-----"
VERIFY_END = "-----END VERIFY.MD-----"

# Realm client secrets from keycloak/quay-realm.json; these must never reach
# Quay's logs (dev-only fixtures — see the repo AGENTS.md security note).
REALM_SECRETS = ["quay-unknown-aud-secret", "quay-obo-secret"]


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Check:
    """One assertion. kind is one of:

    http_get    — GET `url`, expect HTTP `expect`
    http_json   — GET `url`, expect JSON field `field` == `expected`
    log_contains / log_absent — fixed string `literal` in/out of the logs

    required=False downgrades a failure to a warning (does not fail the run).
    """
    id: str
    kind: str
    description: str
    required: bool = True
    url: str = ""
    expect: int = 0
    field: str = ""
    expected: str = ""
    literal: str = ""


def build_checks(base_url: str, keycloak_url: str, keycloak_issuer: str) -> list[Check]:
    checks = [
        Check("quay-health", "http_get", "Quay /health/instance answers 200",
              url=f"{base_url}/health/instance", expect=200),
        Check("keycloak-discovery", "http_get", "Keycloak OIDC discovery document served",
              url=f"{keycloak_url}/.well-known/openid-configuration", expect=200),
        Check("issuer-match", "http_json",
              "discovery issuer matches the issuer Quay is configured with",
              url=f"{keycloak_url}/.well-known/openid-configuration",
              field="issuer", expected=keycloak_issuer),
    ]
    for secret in REALM_SECRETS:
        checks.append(Check(f"no-{secret}", "log_absent",
                            f"realm client secret {secret!r} not in Quay logs",
                            literal=secret))
    return checks


# ---------------------------------------------------------------------------
# Log sources (the compose/OCP reuse seam)
# ---------------------------------------------------------------------------

def compose_files() -> list[str]:
    files = ["compose.yaml"]
    if os.environ.get("QUAY_REPO_ROOT"):
        files.append("compose.build.yaml")
    override = HARNESS_DIR / "compose.override.yaml"
    if override.is_file():
        files.append("compose.override.yaml")
    return files


class ComposeLogSource:
    """Host-side: `docker compose logs quay`."""

    def __init__(self, service: str = "quay"):
        self.docker = os.environ.get("DOCKER", "docker")
        self.service = service

    def fetch(self, since: str | None = None) -> str:
        cmd = [self.docker, "compose"]
        for f in compose_files():
            cmd += ["-f", f]
        cmd += ["logs", "--no-color", "--no-log-prefix"]
        if since:
            cmd += ["--since", since]
        cmd += [self.service]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        return proc.stdout


class OcpLogSource:
    """In-cluster: Kubernetes API with the pod's service-account token.

    Enumerates pods matching the label selector and concatenates their logs.
    Requires RBAC: get+list pods, get pods/log (see ocp/rbac.yaml).
    """

    SA_DIR = pathlib.Path("/var/run/secrets/kubernetes.io/serviceaccount")

    def __init__(self, namespace: str | None = None, label: str | None = None):
        host = os.environ.get("KUBERNETES_SERVICE_HOST")
        port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        if not host:
            raise SystemExit("ERROR: not running inside a Kubernetes pod "
                             "(KUBERNETES_SERVICE_HOST unset); use --log-source compose")
        self.base = f"https://{host}:{port}"
        self.headers = {"Authorization": f"Bearer {(self.SA_DIR / 'token').read_text().strip()}"}
        self.ssl_ctx = ssl.create_default_context(cafile=str(self.SA_DIR / "ca.crt"))
        self.namespace = namespace or (self.SA_DIR / "namespace").read_text().strip()
        self.label = label or os.environ.get("QUAY_POD_LABEL", "app=quay")

    def _get(self, path: str, params: dict | None = None) -> bytes:
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=self.headers)
        with urllib.request.urlopen(req, context=self.ssl_ctx, timeout=60) as resp:
            return resp.read()

    def fetch(self, since: str | None = None) -> str:
        params = {"labelSelector": self.label}
        pods = json.loads(self._get(f"/api/v1/namespaces/{self.namespace}/pods", params))
        names = [p["metadata"]["name"] for p in pods.get("items", [])]
        if not names:
            raise SystemExit(f"ERROR: no pods match label {self.label!r} in namespace "
                             f"{self.namespace}; set QUAY_POD_LABEL accordingly")
        log_params = {}
        if since:
            dt = datetime.datetime.fromisoformat(since.replace("Z", "+00:00"))
            log_params["sinceSeconds"] = str(max(1, int(time.time() - dt.timestamp()) + 60))
        chunks = []
        for name in names:
            chunks.append(self._get(
                f"/api/v1/namespaces/{self.namespace}/pods/{name}/log",
                log_params).decode("utf-8", "replace"))
        return "\n".join(chunks)


class FileLogSource:
    """Replay a previously captured log file."""

    def __init__(self, path: str):
        self.path = pathlib.Path(path)

    def fetch(self, since: str | None = None) -> str:
        return self.path.read_text()


def make_log_source(spec: str, namespace: str | None, label: str | None, service: str):
    if spec == "compose":
        return ComposeLogSource(service=service)
    if spec == "ocp":
        return OcpLogSource(namespace=namespace, label=label)
    if spec.startswith("file:"):
        return FileLogSource(spec[5:])
    raise SystemExit(f"ERROR: unknown log source {spec!r} (compose|ocp|file:PATH)")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def http_get_json(url: str, timeout: float = 30) -> tuple[int, dict | None]:
    # OpenShift Routes serve TLS with the cluster's ingress CA, which is not
    # in the verify Job's trust store — these are health/issuer checks, not
    # confidential channels, so skip verification for https.
    ctx = ssl._create_unverified_context() if url.startswith("https") else None
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=ctx) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default=os.environ.get(
        "QUAY_BASE_URL", f"http://127.0.0.1:{os.environ.get('QUAY_HOST_PORT', '18080')}"))
    parser.add_argument("--keycloak-url", default=os.environ.get(
        "KEYCLOAK_URL", "http://127.0.0.1:18081/realms/quay"),
        help="URL through which the discovery document is fetched")
    parser.add_argument("--keycloak-issuer", default=os.environ.get(
        "KEYCLOAK_ISSUER", "http://quay-keycloak:8080/realms/quay"),
        help="issuer Quay is configured with (must match the discovery doc)")
    parser.add_argument("--log-source", default="compose",
                        help="compose | ocp | file:PATH")
    parser.add_argument("--report-dir", default=os.environ.get(
        "REPORT_DIR", str(HARNESS_DIR / "reports" /
                          f"manual-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}")))
    parser.add_argument("--compose-service", default="quay")
    parser.add_argument("--namespace", default=os.environ.get("OCP_NAMESPACE"))
    parser.add_argument("--pod-label", default=os.environ.get("QUAY_POD_LABEL"))
    parser.add_argument("--print-report", action="store_true",
                        help="print verify.md between marker lines on stdout "
                             "(used by the OCP verify Job)")
    ns = parser.parse_args(argv)

    report_dir = pathlib.Path(ns.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    print(f"Report directory: {report_dir}")

    checks = build_checks(ns.base_url, ns.keycloak_url, ns.keycloak_issuer)
    results: list[tuple[Check, bool, str]] = []

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 1. HTTP checks.
    for check in (c for c in checks if c.kind in ("http_get", "http_json")):
        status, body = http_get_json(check.url)
        if check.kind == "http_get":
            passed = status == check.expect
            detail = "" if passed else f"HTTP {status}, expected {check.expect}"
        else:
            value = (body or {}).get(check.field)
            passed = status == 200 and value == check.expected
            detail = "" if passed else (
                f"{check.field}={value!r}, expected {check.expected!r} (HTTP {status})")
        results.append((check, passed, detail))
        print(f"  [{'PASS' if passed else 'FAIL' if check.required else 'WARN'}] "
              f"{check.description}" + (f" ({detail})" if detail else ""))

    # 2. Log checks.
    source = make_log_source(ns.log_source, ns.namespace, ns.pod_label,
                             ns.compose_service)
    log_text = source.fetch()
    (report_dir / "quay.log").write_text(log_text)
    for check in (c for c in checks if c.kind in ("log_contains", "log_absent")):
        found = check.literal in log_text
        if check.kind == "log_contains":
            passed = found
            detail = "" if passed else f"missing: {check.literal}"
        else:
            passed = not found
            detail = "" if passed else f"leaked: {check.literal}"
        results.append((check, passed, detail))
        print(f"  [{'PASS' if passed else 'FAIL' if check.required else 'WARN'}] "
              f"{check.description}" + (f" ({detail})" if detail else ""))

    write_report(report_dir, ns, stamp, results)

    hard_fail = [r for r in results if not r[1] and r[0].required]
    passed_n = sum(1 for r in results if r[1])
    warn_n = len(results) - passed_n - len(hard_fail)
    print(f"\nSummary:   {passed_n} passed, {len(hard_fail)} failed, {warn_n} warnings")
    print(f"Report:    {report_dir}/verify.md")
    if hard_fail:
        print("\nRESULT: FAIL — see verify.md.")
        return 1
    print("\nRESULT: PASS — stack healthy, OIDC issuer consistent, no secrets in logs.")
    return 0


def write_report(report_dir: pathlib.Path, ns, stamp: str,
                 results: list[tuple[Check, bool, str]]) -> None:
    lines = [
        "# OIDC harness verification", "",
        f"- Date: {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"- Quay: {ns.base_url}",
        f"- Keycloak discovery: {ns.keycloak_url}",
        f"- Expected issuer: {ns.keycloak_issuer}",
        f"- Log source: {ns.log_source}",
        "", "| Result | Check |", "|---|---|",
    ]
    for check, passed, detail in results:
        verdict = "PASS" if passed else ("FAIL" if check.required else "WARN")
        cell = check.description + (f" ({detail})" if detail else "")
        lines.append(f"| {verdict} | {cell} |")
    passed_n = sum(1 for r in results if r[1])
    failed_n = len([r for r in results if not r[1] and r[0].required])
    lines += ["", f"**{passed_n} passed, {failed_n} failed**", ""]
    text = "\n".join(lines)
    (report_dir / "verify.md").write_text(text)

    if ns.print_report:
        print(VERIFY_BEGIN)
        print(text)
        print(VERIFY_END)


if __name__ == "__main__":
    sys.exit(main())
