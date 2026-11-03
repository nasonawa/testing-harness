#!/usr/bin/env python3
"""EDIT-ME product — data-driven verify gate.

Stdlib-only: runs on the host (compose mode) AND inside the OCP verify Job
(python:3.12-slim). The CHECKS table is the extension point — a new
assertion is a new Check(...) entry, nothing else. Reference
implementations: harnesses/quay-ldap/verify.py (login + log-redaction
gate), harnesses/quay-oidc/verify.py (health + issuer match).
"""

from __future__ import annotations

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
import urllib.request

HARNESS_DIR = pathlib.Path(__file__).resolve().parent

VERIFY_BEGIN = "-----BEGIN VERIFY.MD-----"
VERIFY_END = "-----END VERIFY.MD-----"


@dataclasses.dataclass
class Check:
    id: str
    kind: str          # api_login | http_get | http_json | log_contains | log_absent
    description: str
    required: bool = True   # False = warning (reported, never fails the run)
    user: str = ""
    password: str = ""
    expect: int = 0         # expected HTTP status (api_login/http_get)
    url: str = ""           # http_get/http_json target
    json_field: str = ""    # http_json: dotted path into the JSON body
    json_expect: str = ""   # http_json: expected value at that path
    literal: str = ""       # log_*: the substring/pattern to find or forbid
    flags: int = 0          # log_*: re flags (re.S etc.)


def build_checks() -> list[Check]:
    """EDIT-ME checks: the assertions that define what this harness proves.

    Keep them few and meaningful — this table is the contract. Precedence
    matters: put hard preconditions first (a login that must succeed before
    anything downstream can be trusted) and mark soft checks required=False.
    """
    return [
        Check(id="health", kind="http_get",
              description="product health endpoint answers 200",
              url="/", expect=200),
        # Example log gate (the reason most harnesses exist):
        # Check(id="no-secret-leak", kind="log_absent",
        #       description="the fixture secret never appears in logs",
        #       literal=FIXTURE_SECRET),
    ]


# ---------------------------------------------------------------------------
# Log sources — the compose/OCP reuse seam
# ---------------------------------------------------------------------------

def compose_files() -> list[str]:
    files = ["compose.yaml"]
    if os.environ.get("PRODUCT_REPO_ROOT"):
        files.append("compose.build.yaml")
    override = HARNESS_DIR / "compose.override.yaml"
    if override.is_file():
        files.append("compose.override.yaml")
    return files


class ComposeLogSource:
    """Host-side: `docker compose logs [--since TS] <service>`."""

    def __init__(self, service: str = "product"):  # EDIT-ME service name
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
    """In-cluster: Kubernetes API via the mounted ServiceAccount token —
    no oc binary needed. Lists pods by label, concatenates their logs."""

    SA_DIR = pathlib.Path("/var/run/secrets/kubernetes.io/serviceaccount")

    def __init__(self, namespace: str, label: str):
        self.namespace = namespace
        self.label = label

    def _req(self, path: str) -> dict | str:
        token = (self.SA_DIR / "token").read_text()
        ctx = ssl.create_default_context(cafile=str(self.SA_DIR / "ca.crt"))
        url = f"https://kubernetes.default.svc{path}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, context=ctx) as resp:
            body = resp.read().decode()
        try:
            return json.loads(body)
        except ValueError:
            return body

    def fetch(self, since: str | None = None) -> str:
        selector = self.label.replace("=", "%3D")
        pods = self._req(f"/api/v1/namespaces/{self.namespace}/pods?labelSelector={selector}")
        chunks = []
        for pod in pods.get("items", []):
            name = pod["metadata"]["name"]
            chunks.append(self._req(
                f"/api/v1/namespaces/{self.namespace}/pods/{name}/log"))
        return "\n".join(chunks)


class FileLogSource:
    """Replay a captured log file: --log-source file:PATH."""

    def __init__(self, path: str):
        self.path = pathlib.Path(path)

    def fetch(self, since: str | None = None) -> str:
        return self.path.read_text(errors="replace")


def make_log_source(spec: str, namespace: str | None, label: str | None, service: str):
    if spec == "compose":
        return ComposeLogSource(service)
    if spec == "ocp":
        return OcpLogSource(namespace or "", label or "")
    if spec.startswith("file:"):
        return FileLogSource(spec[5:])
    sys.exit(f"ERROR: unknown --log-source {spec!r}")


# ---------------------------------------------------------------------------
# Check executors
# ---------------------------------------------------------------------------

def http_get(base_url: str, check: Check) -> int:
    try:
        with urllib.request.urlopen(base_url + check.url, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def http_json(base_url: str, check: Check) -> tuple[int, object]:
    try:
        with urllib.request.urlopen(base_url + check.url, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None


def run_checks(checks: list[Check], base_url: str, logs: str) -> list[tuple[Check, str, bool]]:
    results = []
    for check in checks:
        if check.kind == "http_get":
            status = http_get(base_url, check)
            ok = status == check.expect
            results.append((check, f"HTTP {status}, expected {check.expect}", ok))
        elif check.kind == "http_json":
            status, body = http_json(base_url, check)
            value = body
            for part in check.json_field.split("."):
                value = (value or {}).get(part)
            ok = status == check.expect and value == check.json_expect
            results.append((check, f"{check.json_field}={value!r}, expected "
                                   f"{check.json_expect!r} (HTTP {status})", ok))
        elif check.kind == "log_contains":
            ok = re.search(check.literal, logs, check.flags) is not None
            results.append((check, "pattern present" if ok else "pattern MISSING", ok))
        elif check.kind == "log_absent":
            ok = re.search(check.literal, logs, check.flags) is None
            results.append((check, "absent" if ok else "LEAKED/FOUND", ok))
        else:
            results.append((check, f"unknown kind {check.kind}", False))
    return results


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def write_report(report_dir: pathlib.Path, base_url: str, log_source: str,
                 since: str, results: list[tuple[Check, str, bool]]) -> str:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    required_fail = sum(1 for c, _, ok in results if not ok and c.required)
    warn = sum(1 for c, _, ok in results if not ok and not c.required)
    lines = [
        "# EDIT-ME product — verify results", "",
        f"- Date: {stamp}",
        f"- Target: {base_url} (log source: {log_source}, since: {since or 'all'})",
        "",
        "| Result | Check | Detail |", "|---|---|---|",
    ]
    for check, detail, ok in results:
        marker = "PASS" if ok else ("WARN" if not check.required else "FAIL")
        lines.append(f"| {marker} | {check.description} | {detail} |")
    lines += ["", f"**{len(results) - required_fail - warn} passed, "
                   f"{required_fail} failed, {warn} warnings**"]
    text = "\n".join(lines) + "\n"
    (report_dir / "verify.md").write_text(text)
    return text


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.environ.get(
        "PRODUCT_BASE_URL", "http://127.0.0.1:18080"))  # EDIT-ME default port
    parser.add_argument("--log-source", default="compose",
                        help="compose | ocp | file:PATH")
    parser.add_argument("--namespace", default=os.environ.get("K8S_NAMESPACE"))
    parser.add_argument("--pod-label", default=os.environ.get("PRODUCT_POD_LABEL"))
    parser.add_argument("--service", default="product")  # EDIT-ME compose service
    parser.add_argument("--since", default=None,
                        help="only fetch log lines newer than this timestamp")
    parser.add_argument("--report-dir", default=str(HARNESS_DIR / "reports" / "manual"))
    args = parser.parse_args(argv)

    base_url = args.base_url.rstrip("/")
    checks = build_checks()
    needs_logs = any(c.kind.startswith("log_") for c in checks)
    logs = ""
    if needs_logs:
        since = args.since or datetime.datetime.now(
            datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        logs = make_log_source(args.log_source, args.namespace, args.pod_label,
                               args.service).fetch(since)
    else:
        since = None

    results = run_checks(checks, base_url, logs)
    text = write_report(pathlib.Path(args.report_dir), base_url,
                        args.log_source, since or "-", results)
    print(VERIFY_BEGIN)
    print(text)
    print(VERIFY_END)
    return 1 if any(not ok and c.required for c, _, ok in results) else 0


if __name__ == "__main__":
    sys.exit(main())
