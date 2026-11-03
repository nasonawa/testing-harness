#!/usr/bin/env python3
"""PROJQUAY-13372 redaction verification (data-driven port of verify.sh).

Triggers LDAP logins against the harness Quay (valid credentials, an invalid
password, and a user whose userPassword is a PBKDF2 hash), then scans the
quay container's stdout — the same stream the ticket's "gunicorn-web stdout"
log came from — for leaks.

With USERS_DEBUG=1 the python-ldap trace (trace_level 2) prints every
operation's arguments and results, and DEBUGLOG=true raises the Python root
logger so the verify_credentials "DN ... found:" debug line fires. Both must
show password values redacted while keeping every other attribute visible
(per the PR #7845 review: only remove password values).

The PBKDF2 hash literal lives in exactly one place — ldap/base.ldif (uid=
hashuser) — and is extracted here at runtime, so the fixture and the
assertion can never drift apart.

Runs in two environments (the --log-source seam):
  * compose: on the host, `docker compose logs quay` (default)
  * ocp:     inside the OpenShift verify Job, via the Kubernetes API using
             the pod's service-account token (needs pods + pods/log get)
  * file:PATH: replay a previously captured log

Extension point: add regression checks by appending entries to CHECKS in
build_checks() below — no control-flow changes needed.

Usage:
  verify.py [--base-url URL] [--log-source compose|ocp|file:PATH]
            [--ldif ldap/base.ldif] [--report-dir DIR] [--print-report]
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


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Check:
    """One assertion. kind is one of:

    api_login     — POST /api/v1/signin with user/password, expect `expect`
                    (drives the LDAP flows whose logs the other checks scan)
    log_contains  — the fixed string `literal` must appear in the login-window
                    log (a required redaction/mask shape)
    log_absent    — the fixed string `literal` must NOT appear (a leak)

    required=False downgrades a failure to a warning (does not fail the run).
    """
    id: str
    kind: str
    description: str
    required: bool = True
    user: str = ""
    password: str = ""
    expect: int = 0
    literal: str = ""


def ldif_userpassword(ldif_path: pathlib.Path, uid: str) -> str:
    """Single source of truth: extract uid's userPassword from the LDIF.

    Raises loudly when absent so drift between fixture and assertion fails
    the gate instead of silently making the no-leak check vacuous.
    """
    text = ldif_path.read_text()
    m = re.search(rf"dn: uid={re.escape(uid)},.*?^userPassword: (\S+)",
                  text, re.S | re.M)
    if not m:
        raise SystemExit(f"ERROR: userPassword for uid={uid} not found in {ldif_path}")
    return m.group(1)


def build_checks(ldif_path: pathlib.Path) -> list[Check]:
    pbkdf2 = ldif_userpassword(ldif_path, "hashuser")
    return [
        # --- logins: drive the Directory Manager bind, the user search (whose
        #     result includes userPassword) and the user bind ---
        # Sign in through the same endpoint the Playwright API client uses
        # (utils/api/raw-client.ts): fetch a CSRF token, then POST
        # /api/v1/signin. The registry's /v2/ basic auth is NOT suitable: it
        # expects a signed JWT bearer token, so raw credentials never reach
        # LDAP through it.
        Check("login-valid", "api_login", "valid LDAP login (testuser_ldap)",
              user="testuser_ldap", password="password", expect=200),
        Check("login-wrongpass", "api_login", "invalid password rejected",
              user="testuser_ldap", password="wrongpass", expect=403, required=False),
        Check("login-hashuser", "api_login",
              "PBKDF2 hash is not a bind password (hashuser)",
              user="hashuser", password="guessme", expect=403, required=False),

        # --- positives: the fix's required output shapes (produced by
        #     _LDAPTraceRedactor / _redact_password_attributes) ---
        Check("trace-mask", "log_contains",
              "python-ldap trace masks userPassword values",
              literal="'userPassword': [b'******']"),
        Check("debug-mask", "log_contains",
              "verify_credentials debug log masks password values",
              literal="'userPassword': ['******']"),
        Check("debug-keeps-attrs", "log_contains",
              "debug log keeps non-password attributes (review requirement)",
              literal="b'testuser_ldap@example.com'"),
        Check("bind-mask", "log_contains",
              "bind tuples show the redaction mask",
              literal="'******', None, None)"),

        # --- negatives: the leaks from the PROJQUAY-13372 ticket ---
        Check("no-plaintext", "log_absent",
              "no raw plaintext userPassword value",
              literal="[b'password']"),
        Check("no-pbkdf2", "log_absent",
              "no PBKDF2 userPassword hash (ticket's leak)",
              literal=pbkdf2),
        Check("no-bind-password", "log_absent",
              "no user password in bind tuples",
              literal="'password', None, None)"),
        Check("no-admin-bind", "log_absent",
              "no Directory Manager password in bind tuples",
              literal="'admin', None, None)"),
    ]


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
    """Host-side: `docker compose logs [--since TS] quay`."""

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
# API login (port of verify.sh login(), minus the sed JSON scraping)
# ---------------------------------------------------------------------------

def login(base_url: str, user: str, password: str) -> int:
    """CSRF token + POST /api/v1/signin; returns the HTTP status code."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        with opener.open(urllib.request.Request(
                f"{base_url}/csrf_token",
                headers={"X-Requested-With": "XMLHttpRequest"}), timeout=30) as resp:
            token = json.loads(resp.read())["csrf_token"]
    except Exception:
        return 0
    req = urllib.request.Request(
        f"{base_url}/api/v1/signin", method="POST",
        data=json.dumps({"username": user, "password": password}).encode(),
        headers={"X-CSRF-Token": token, "Content-Type": "application/json"})
    try:
        opener.open(req, timeout=60)
        return 200
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def http_ok(url: str, timeout: float = 5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default=os.environ.get(
        "QUAY_BASE_URL", f"http://127.0.0.1:{os.environ.get('QUAY_HOST_PORT', '18090')}"))
    parser.add_argument("--log-source", default="compose",
                        help="compose | ocp | file:PATH")
    parser.add_argument("--ldif", default=str(HARNESS_DIR / "ldap" / "base.ldif"))
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

    checks = build_checks(pathlib.Path(ns.ldif))
    results: list[tuple[Check, bool, str]] = []  # (check, passed, detail)

    # 1. Quay must be up, or everything below is vacuous.
    print("[verify] checking Quay is up ...")
    if not http_ok(f"{ns.base_url}/health/instance"):
        print(f"ERROR: Quay is not healthy on {ns.base_url}; run ./run.sh up first.",
              file=sys.stderr)
        return 1

    # Only look at log lines from this point on.
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 2. Trigger the LDAP flows.
    for check in (c for c in checks if c.kind == "api_login"):
        print(f"[verify] {check.description} ({check.user}:{check.password}) ...")
        code = login(ns.base_url, check.user, check.password)
        print(f"[verify]   -> HTTP {code} (expected {check.expect})")
        passed = code == check.expect
        results.append((check, passed, f"HTTP {code}, expected {check.expect}"))
        if not passed and check.required:
            print("ERROR: expected login failed; the redaction checks below would "
                  "be vacuous.", file=sys.stderr)
            write_report(report_dir, ns, stamp, results, aborted=True)
            return 1

    # Give the workers a moment to flush the trace and debug output.
    time.sleep(3)

    # 3. Capture the logs (login window + full, for context).
    source = make_log_source(ns.log_source, ns.namespace, ns.pod_label,
                             ns.compose_service)
    since_log = source.fetch(since=stamp)
    (report_dir / "quay-since.log").write_text(since_log)
    full_log = source.fetch()
    (report_dir / "quay.log").write_text(full_log)
    if not since_log.strip():
        print("ERROR: no Quay logs captured; is the stack running?", file=sys.stderr)
        return 1

    # 4. Scan the login window for the required/forbidden shapes.
    for check in (c for c in checks if c.kind in ("log_contains", "log_absent")):
        found = check.literal in since_log
        if check.kind == "log_contains":
            passed = found
            detail = "" if found else f"missing: {check.literal}"
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
    print(f"Full logs: {report_dir}/quay-since.log, {report_dir}/quay.log")
    print(f"Report:    {report_dir}/verify.md")

    if hard_fail:
        print("\nRESULT: LEAK DETECTED (or expected log lines missing).")
        print(f"Inspect {report_dir}/quay-since.log for the failing patterns.")
        return 1
    print("\nRESULT: PASS — LDAP passwords are redacted; all other attributes "
          "remain in the logs.")
    return 0


def write_report(report_dir: pathlib.Path, ns, stamp: str,
                 results: list[tuple[Check, bool, str]], aborted: bool = False) -> None:
    lines = [
        "# PROJQUAY-13372 redaction verification", "",
        f"- Date: {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"- Login window since: {stamp}",
        f"- Quay: {ns.base_url} (USERS_DEBUG=1, DEBUGLOG=true)",
        f"- Log source: {ns.log_source}",
    ]
    if aborted:
        lines += ["", "**ABORTED: a required login failed; log checks were not run.**"]
    lines += ["", "| Result | Check |", "|---|---|"]
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
