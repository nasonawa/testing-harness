#!/usr/bin/env python3
"""Orchestrator for the containerized OIDC Playwright harness.

Standalone port of the original .cursor/oidc-testing/run.sh, rewritten in
stdlib-only Python so the harness is self-contained and agent-extensible.

Image source:
  * pull mode (default): run ${QUAY_IMAGE}, a published Quay image. Build
    support is degraded (no quay-builder binary) and build specs cannot run.
  * source mode: set QUAY_REPO_ROOT to a quay checkout; every compose
    invocation then includes compose.build.yaml, which builds the quay (and
    repomirror) image from that repo instead, and run.py extracts the
    quay-builder binary for build support.

The Playwright runner always bind-mounts the suite from
${QUAY_REPO_ROOT}/web, so `test` requires QUAY_REPO_ROOT in either mode.

Usage: ./run.py <command> [args...]   (./run.sh is a thin shim)
"""

from __future__ import annotations

import argparse
import datetime
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import urllib.request

HARNESS_DIR = pathlib.Path(__file__).resolve().parent
REPORTS_DIR = HARNESS_DIR / "reports"

# Lowest-precedence defaults. Layering: DEFAULTS < .env < os.environ < CLI.
DEFAULTS = {
    "DOCKER": "docker",
    "QUAY_IMAGE": "quay.io/projectquay/quay:latest",
    "QUAY_BUILT_IMAGE": "localhost/quay-oidc-testing-quay:latest",
    "QUAY_REPO_ROOT": "",
    "KEYCLOAK_IMAGE": "quay.io/keycloak/keycloak:26.2",
    "QUAY_HOST_PORT": "18080",
    "KEYCLOAK_HOST_PORT": "18081",
    "MAILPIT_HOST_PORT": "18025",
    "JAEGER_HOST_PORT": "16686",
    "GARAGE_HOST_PORT": "13900",
    "QUAY_DOCKER_SOCKET": "",  # "" = auto-detect from DOCKER_HOST/context
    "DOCKER_USER": "",  # "" = auto-detect
    "RUN_LABEL": "",
    "REPORT_DIR": "",
    # OpenShift variant (ocp subcommands)
    "OCP_NAMESPACE": "quay-oidc-harness",
    "OCP_QUAYREGISTRY_NAME": "quay",
    "QUAY_POD_LABEL": "app=quay",
    "QUAY_DEPLOYMENT": "quay-quay-app",  # operator convention: <name>-quay-app
}

OCP_DIR = HARNESS_DIR / "ocp"
OCP_GENERATED_DIR = OCP_DIR / "generated"

USAGE = """\
Usage: ./run.sh <command> [args...]

Commands:
  build               Fetch registry CLIs, then build the Quay and Playwright
                      images (source mode) / pull QUAY_IMAGE (pull mode).
  fetch [--force]     Download crane/regctl/oras/cosign into ./bin (build does
                      this automatically; --force re-downloads).
  up                  Start the full CI-parity stack (db/redis/mailpit/keycloak/
                      clair-db/garage/jaeger, then quay, clair, repomirror) and
                      wait for readiness.
  verify              Light health/OIDC checks against the running stack. The
                      real verification is the Playwright suite (`test`).
  test [args...]      Run the Playwright suite in the runner container.
                      With no args: full suite minus the upgrade lane.
                      Extra args are passed to `npx playwright test`, e.g.:
                        ./run.sh test --grep "@auth:OIDC"
  report              Regenerate summary.md from the latest run's results.json
                      (pass REPORT_DIR=... to target an archived run).
  shell               Open a bash shell in the runner container.
  logs [service]      Follow compose logs.
  down                Stop containers, keep volumes and reports.
  clean               down -v and remove generated reports (resets Keycloak
                      identities — see README before running this).
  doctor              Print the resolved configuration and environment checks.
  ocp <subcommand>    OpenShift variant (operator-installed Quay):
                        render   print the rendered kustomize manifests
                        apply    deploy namespace + Keycloak + QuayRegistry,
                                 wait for the registry to become Available
                        verify   run the verify Job against the Quay Route
                        logs     follow the quay-app deployment logs
                        delete   remove everything the harness created
  help                Show this help.

Environment (optionally via .env, see .env.example): DOCKER, KEYCLOAK_IMAGE,
QUAY_IMAGE, QUAY_REPO_ROOT, QUAY_BUILT_IMAGE, QUAY_HOST_PORT,
KEYCLOAK_HOST_PORT, MAILPIT_HOST_PORT, JAEGER_HOST_PORT, GARAGE_HOST_PORT,
QUAY_DOCKER_SOCKET, DOCKER_USER, REPORT_DIR, RUN_LABEL, OCP_NAMESPACE,
QUAY_POD_LABEL.
"""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config() -> dict:
    cfg = dict(DEFAULTS)
    env_file = HARNESS_DIR / ".env"
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)", line)
            if not m:
                continue
            key, val = m.group(1), m.group(2).strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            else:
                val = val.split(" #")[0].strip()
            if key in cfg:
                cfg[key] = val
    for key in cfg:
        if key in os.environ:
            cfg[key] = os.environ[key]
    return cfg


def source_mode(cfg: dict) -> bool:
    return bool(cfg["QUAY_REPO_ROOT"])


def compose_files(cfg: dict) -> list[str]:
    files = ["compose.yaml"]
    if source_mode(cfg):
        files.append("compose.build.yaml")
    override = HARNESS_DIR / "compose.override.yaml"
    if override.is_file():
        files.append("compose.override.yaml")  # user extension point
    return files


def export_env(cfg: dict) -> None:
    for key, val in cfg.items():
        if val != "":
            os.environ[key] = val


# ---------------------------------------------------------------------------
# Process / HTTP helpers
# ---------------------------------------------------------------------------

def sh(cmd: list[str], *, check: bool = True, **kw) -> subprocess.CompletedProcess:
    print(f"+ {' '.join(str(c) for c in cmd)}", file=sys.stderr)
    return subprocess.run([str(c) for c in cmd], check=check, **kw)


def try_run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **kw)


def http_ok(url: str, timeout: float = 5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except Exception:
        return False


def wait_http(url: str, label: str, tries: int, delay: float, hint: str) -> None:
    print(f"Waiting for {label} at {url} ...")
    for _ in range(tries):
        if http_ok(url, timeout=3):
            print(f"{label} is ready.")
            return
        time.sleep(delay)
    sys.exit(f"ERROR: {label} did not become ready. Check: ./run.sh logs\n{hint}")


def detect_docker_user(cfg: dict) -> str:
    """Port of the run.sh rootless-detection block.

    Under rootless Docker/Podman, container uid 0 is already mapped to the
    invoking host user, while the host uid itself is outside the container's
    subordinate uid range: passing it as `user:` makes runc fail with
    "unable to setup user: chown /dev/stdin: invalid argument". So use 0:0
    for rootless engines (bind-mounted reports are still owned by the host
    user) and the real uid:gid for rootful ones.
    """
    if cfg["DOCKER_USER"]:
        return cfg["DOCKER_USER"]
    info = try_run([cfg["DOCKER"], "info", "--format", "{{.SecurityOptions}}"])
    if "rootless" in (info.stdout or ""):
        return "0:0"
    return f"{os.getuid()}:{os.getgid()}"


def resolve_docker_socket(cfg: dict) -> str:
    """Build support needs the runtime socket inside the Quay container.

    Resolve DOCKER_HOST (rootless Docker puts it under /run/user/<uid>) unless
    the caller pinned QUAY_DOCKER_SOCKET. Falls back to the conventional path
    so commands that do not start Quay keep working on unusual setups.
    """
    if cfg["QUAY_DOCKER_SOCKET"]:
        return cfg["QUAY_DOCKER_SOCKET"]
    host = os.environ.get("DOCKER_HOST", "")
    if host.startswith("unix://"):
        return host[len("unix://"):]
    if host.startswith(("tcp://", "ssh://")):
        return ""  # cannot mount a non-local socket
    ctx = try_run([cfg["DOCKER"], "context", "inspect", "-f",
                   "{{.Endpoints.docker.Host}}"])
    if ctx.returncode == 0 and ctx.stdout.startswith("unix://"):
        return ctx.stdout.strip()[len("unix://"):]
    if pathlib.Path("/var/run/docker.sock").is_socket():
        return "/var/run/docker.sock"
    return ""


def use_aliases(cfg: dict) -> list[str]:
    help_text = try_run(compose_base(cfg) + ["run", "--help"]).stdout or ""
    return ["--use-aliases"] if "--use-aliases" in help_text else []


# ---------------------------------------------------------------------------
# Compose plumbing
# ---------------------------------------------------------------------------

def compose_base(cfg: dict) -> list[str]:
    files = compose_files(cfg)
    return [cfg["DOCKER"], "compose", *(arg for f in files for arg in ("-f", f))]


def compose(cfg: dict, *args, profiles: bool = True, **kw) -> subprocess.CompletedProcess:
    cmd = compose_base(cfg)
    if profiles:
        cmd += ["--profile", "test"]
    cmd += [str(a) for a in args]
    return sh(cmd, **kw)


def compose_quiet(cfg: dict, *args) -> subprocess.CompletedProcess:
    return try_run(compose_base(cfg) + ["--profile", "test"] + [str(a) for a in args])


# ---------------------------------------------------------------------------
# Report archiving (port of new_report_dir)
# ---------------------------------------------------------------------------

def sanitize_label(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-")


def new_report_dir(cfg: dict) -> pathlib.Path:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    label = sanitize_label(cfg["RUN_LABEL"])
    dirname = f"{stamp}-{label}" if label else stamp
    report_dir = REPORTS_DIR / "runs" / dirname
    for sub in ("playwright-report", "test-results", "summary"):
        (report_dir / sub).mkdir(parents=True, exist_ok=True)
    latest = REPORTS_DIR / "latest"
    tmp = REPORTS_DIR / "latest.tmp"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(report_dir)
    tmp.replace(latest)
    cfg["REPORT_DIR"] = str(report_dir)
    os.environ["REPORT_DIR"] = str(report_dir)
    print(f"Reports for this run: {report_dir.relative_to(HARNESS_DIR)}")
    return report_dir


def latest_report_dir() -> pathlib.Path | None:
    runs = REPORTS_DIR / "runs"
    if not runs.is_dir():
        return None
    dirs = sorted((d for d in runs.iterdir() if d.is_dir()), key=lambda d: d.name)
    return dirs[-1] if dirs else None


# ---------------------------------------------------------------------------
# Harness-specific setup
# ---------------------------------------------------------------------------

def check_repo_root(cfg: dict, *, for_what: str) -> pathlib.Path:
    root = pathlib.Path(cfg["QUAY_REPO_ROOT"]).expanduser() if cfg["QUAY_REPO_ROOT"] else None
    if root is None or not (root / "web" / "playwright").is_dir():
        sys.exit(
            f"ERROR: {for_what} requires QUAY_REPO_ROOT pointing at a quay checkout\n"
            f"(the Playwright suite is bind-mounted from <QUAY_REPO_ROOT>/web).\n"
            f"Set it in .env or the environment, e.g. "
            f"QUAY_REPO_ROOT=/path/to/quay"
        )
    return root


def ensure_builder_binary(cfg: dict) -> bool:
    """Extract the quay-builder binary the PopenExecutor forks for builds.

    Source mode only: `local-dev-extract-builder` is a make target in the
    quay checkout. Returns False (after warning) in pull mode.
    """
    if not source_mode(cfg):
        print("WARNING: pull mode (QUAY_REPO_ROOT unset) — no quay-builder binary;")
        print("         build specs are not runnable. Grep-invert them or set")
        print("         QUAY_REPO_ROOT for build support.")
        return False
    root = check_repo_root(cfg, for_what="builder extraction")
    builder = root / "local-dev" / "quay-builder"
    if not builder.is_file():
        print("Extracting quay-builder binary from the builder image...")
        sh(["make", "-C", str(root), "local-dev-extract-builder",
            f"DOCKER={cfg['DOCKER']}"])
    if not builder.is_file():
        print(f"ERROR: {builder} is missing; build support will not work.",
              file=sys.stderr)
        return False
    return True


def init_garage(cfg: dict) -> None:
    """Garage needs a layout, the two geo-replication buckets, and the fixed
    access key referenced by config/stack/config.yaml. The upstream helper
    (copied verbatim from the quay repo's local-dev) accepts a container ID
    in GARAGE_CONTAINER, so compose service names stay anonymous."""
    cid = compose_quiet(cfg, "ps", "-q", "garage").stdout.strip()
    if not cid:
        sys.exit("ERROR: garage container is not running")
    env = dict(os.environ, GARAGE_CONTAINER=cid, CONTAINER_RUNTIME=cfg["DOCKER"])
    subprocess.run(["bash", str(HARNESS_DIR / "config" / "garage" / "init-garage.sh")],
                   check=True, env=env)


def wait_for_garage(cfg: dict) -> None:
    print("Waiting for Garage ...")
    for _ in range(60):
        probe = compose_quiet(cfg, "exec", "-T", "garage", "/garage", "status")
        if probe.returncode == 0:
            print("Garage is ready.")
            return
        time.sleep(2)
    sys.exit("ERROR: Garage did not become healthy. Check: ./run.sh logs garage")


def wait_for_clair(cfg: dict) -> None:
    # Clair shares Quay's network namespace, so probe from inside quay.
    print("Waiting for Clair on http://localhost:6000/indexer/api/v1/index_state ...")
    for _ in range(60):
        probe = compose_quiet(cfg, "exec", "-T", "quay", "python3", "-c",
                              "import urllib.request; urllib.request.urlopen("
                              "'http://127.0.0.1:6000/indexer/api/v1/index_state', timeout=5)")
        if probe.returncode == 0:
            print("Clair is ready.")
            return
        time.sleep(3)
    sys.exit("ERROR: Clair did not become healthy. Check: ./run.sh logs quay")


def wait_for_jaeger(cfg: dict) -> None:
    url = f"http://127.0.0.1:{cfg['JAEGER_HOST_PORT']}/api/services"
    if http_ok(url, timeout=3):
        print("Jaeger is ready.")
    else:
        print(f"WARNING: Jaeger is not answering on {url}; tracing spans will "
              "be dropped.", file=sys.stderr)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_build(cfg: dict, args: list[str]) -> int:
    sh([str(HARNESS_DIR / "fetch-tools.sh")])
    if source_mode(cfg):
        check_repo_root(cfg, for_what="source-mode build")
        ensure_builder_binary(cfg)
        compose(cfg, "build", *args)
    else:
        print("Pull mode (QUAY_REPO_ROOT unset): pulling QUAY_IMAGE, nothing to build.")
        print("Note: `test` needs QUAY_REPO_ROOT even in pull mode (the suite")
        print("lives in the quay checkout's web/playwright), and build specs")
        print("need it for the quay-builder binary.")
        compose(cfg, "pull", "quay")
    return 0


def cmd_fetch(cfg: dict, args: list[str]) -> int:
    sh([str(HARNESS_DIR / "fetch-tools.sh"), *args])
    return 0


def cmd_up(cfg: dict, args: list[str]) -> int:
    if args:
        compose(cfg, "up", *args)
        return 0

    cfg["QUAY_DOCKER_SOCKET"] = resolve_docker_socket(cfg) or "/var/run/docker.sock"
    os.environ["QUAY_DOCKER_SOCKET"] = cfg["QUAY_DOCKER_SOCKET"]
    builder_ok = ensure_builder_binary(cfg)
    if builder_ok and not pathlib.Path(cfg["QUAY_DOCKER_SOCKET"]).is_socket():
        sys.exit(f"ERROR: container runtime socket '{cfg['QUAY_DOCKER_SOCKET']}' not "
                 "found.\n       Set QUAY_DOCKER_SOCKET to the host path of the "
                 "Docker/Podman socket.")
    if not builder_ok and not pathlib.Path(cfg["QUAY_DOCKER_SOCKET"]).is_socket():
        print(f"WARNING: runtime socket '{cfg['QUAY_DOCKER_SOCKET']}' not found "
              "(build support is inactive anyway in pull mode).")

    # Start everything Quay depends on, plus the optional services whose
    # containers do not share Quay's network namespace.
    compose(cfg, "up", "-d", "quay-db", "quay-redis", "quay-mailpit",
            "quay-keycloak", "clair-db", "garage", "jaeger", profiles=False)
    wait_for_garage(cfg)
    init_garage(cfg)
    wait_for_jaeger(cfg)
    compose(cfg, "up", "-d", "quay", profiles=False)
    wait_http(f"http://127.0.0.1:{cfg['QUAY_HOST_PORT']}/health/instance",
              "Quay", tries=120, delay=5,
              hint="First boot runs migrations (start_period 300s).")
    # Clair and repomirror join Quay's network namespace, so they start after it.
    compose(cfg, "up", "-d", "clair", profiles=False)
    wait_for_clair(cfg)
    compose(cfg, "up", "-d", "repomirror", profiles=False)
    print(f"Quay:      http://localhost:{cfg['QUAY_HOST_PORT']}")
    print(f"Keycloak:  http://localhost:{cfg['KEYCLOAK_HOST_PORT']} (API checks)")
    print(f"Mailpit:   http://localhost:{cfg['MAILPIT_HOST_PORT']}")
    print(f"Jaeger:    http://localhost:{cfg['JAEGER_HOST_PORT']}")
    print(f"Garage:    http://localhost:{cfg['GARAGE_HOST_PORT']}")
    print("Clair:     http://localhost:6000 (inside Quay's network namespace)")
    print("Mode:      " + ("source (built from QUAY_REPO_ROOT)" if source_mode(cfg)
                           else f"pull ({cfg['QUAY_IMAGE']})"))
    print("Next:      ./run.sh test")
    return 0


def cmd_verify(cfg: dict, args: list[str]) -> int:
    print("Note: the OIDC harness's real verification is the Playwright suite")
    print("(./run.sh test). Running the light health/OIDC checks.\n")
    report_dir = pathlib.Path(cfg["REPORT_DIR"]) if cfg["REPORT_DIR"] else new_report_dir(cfg)
    sys.path.insert(0, str(HARNESS_DIR))
    import verify

    argv = [
        "--base-url", f"http://127.0.0.1:{cfg['QUAY_HOST_PORT']}",
        "--keycloak-issuer", f"http://quay-keycloak:8080/realms/quay",
        "--keycloak-url", f"http://localhost:{cfg['KEYCLOAK_HOST_PORT']}/realms/quay",
        "--log-source", "compose",
        "--report-dir", str(report_dir),
        *args,
    ]
    return verify.main(argv)


def cmd_test(cfg: dict, args: list[str]) -> int:
    check_repo_root(cfg, for_what="test")
    report_dir = new_report_dir(cfg)
    (report_dir / "logs").mkdir(exist_ok=True)

    run_cmd = compose_base(cfg) + ["--profile", "test", "run", "--rm", *use_aliases(cfg), "playwright"]
    if args:
        run_cmd += ["npx", "playwright", "test", *args]
    print(f"+ {' '.join(run_cmd)}", file=sys.stderr)

    log_path = report_dir / "logs" / "playwright.log"
    # Stream the runner's output live to the console and to the log file
    # (the container is removed by --rm, so this is the only transcript).
    with log_path.open("w") as log:
        proc = subprocess.Popen(run_cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            log.write(line)
        status = proc.wait()

    # Quay's container persists after the run; dump its logs for post-run RCA.
    dump = compose_quiet(cfg, "logs", "--no-color", "--no-log-prefix", "quay")
    (report_dir / "logs" / "quay.log").write_text(dump.stdout or "")
    rel = report_dir.relative_to(HARNESS_DIR)
    print(f"Logs:    {rel}/logs/{{quay,playwright}}.log")
    print(f"Report:  {rel}/playwright-report (reports/latest points here)")
    return status


def cmd_report(cfg: dict, args: list[str]) -> int:
    report_dir = cfg["REPORT_DIR"] or (str(latest_report_dir()) if latest_report_dir() else "")
    if report_dir.startswith("./") or (report_dir and not report_dir.startswith("/")):
        # Compose resolves relative bind-mount paths from its own directory.
        report_dir = str(HARNESS_DIR / report_dir.removeprefix("./"))
    if not report_dir or not (pathlib.Path(report_dir) / "test-results" / "results.json").is_file():
        sys.exit("No test results found under reports/runs; run ./run.sh test first.")
    os.environ["REPORT_DIR"] = report_dir
    # Bypass the entrypoint so this works even when the stack is stopped.
    compose(cfg, "run", "--rm", "--no-deps", "--entrypoint", "node",
            "playwright", "/usr/local/bin/summarize.mjs")
    print(f"Report: {pathlib.Path(report_dir).relative_to(HARNESS_DIR)}/playwright-report")
    print(f"Open:   npx playwright show-report {report_dir}/playwright-report")
    return 0


def cmd_shell(cfg: dict, args: list[str]) -> int:
    report_dir = cfg["REPORT_DIR"] or (str(latest_report_dir()) if latest_report_dir() else "")
    if not report_dir:
        report_dir = str(new_report_dir(cfg))
    os.environ["REPORT_DIR"] = report_dir
    compose(cfg, "run", "--rm", *use_aliases(cfg), "--entrypoint", "bash", "playwright")
    return 0


def cmd_logs(cfg: dict, args: list[str]) -> int:
    compose(cfg, "logs", "-f", *args)
    return 0


def cmd_down(cfg: dict, args: list[str]) -> int:
    compose(cfg, "down", *args)
    return 0


def cmd_clean(cfg: dict, args: list[str]) -> int:
    compose(cfg, "down", "-v", *args)
    if REPORTS_DIR.exists():
        shutil.rmtree(REPORTS_DIR)  # removes every archived run incl. reports/latest
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    print("WARNING: `clean` also wiped the keycloak-data volume — every `up`")
    print("re-imports the realm with the pinned UUIDs, but external logins")
    print("recorded in PostgreSQL no longer match; see README (Keycloak")
    print("identity stability).")
    return 0


def cmd_doctor(cfg: dict, args: list[str]) -> int:
    cfg["DOCKER_USER"] = detect_docker_user(cfg)
    cfg["QUAY_DOCKER_SOCKET"] = resolve_docker_socket(cfg) or "/var/run/docker.sock"
    quay_root = pathlib.Path(cfg["QUAY_REPO_ROOT"]).expanduser() if cfg["QUAY_REPO_ROOT"] else None
    builder = quay_root / "local-dev" / "quay-builder" if quay_root else None
    rows = [
        ("mode", "source (build from QUAY_REPO_ROOT)" if source_mode(cfg) else "pull (published image)"),
        ("QUAY_IMAGE", cfg["QUAY_IMAGE"]),
        ("QUAY_BUILT_IMAGE", cfg["QUAY_BUILT_IMAGE"] + "  (source mode)"),
        ("QUAY_REPO_ROOT", cfg["QUAY_REPO_ROOT"] or "<unset>"),
        ("  web/playwright present", "yes" if quay_root and (quay_root / "web" / "playwright").is_dir()
         else ("no" if quay_root else "n/a")),
        ("  quay-builder binary", "present" if builder and builder.is_file()
         else ("missing (run up/build)" if builder else "n/a (pull mode)")),
        ("KEYCLOAK_IMAGE", cfg["KEYCLOAK_IMAGE"]),
        ("DOCKER", cfg["DOCKER"]),
        ("DOCKER_USER (runner)", cfg["DOCKER_USER"]),
        ("QUAY_DOCKER_SOCKET", cfg["QUAY_DOCKER_SOCKET"]),
        ("compose files", ", ".join(compose_files(cfg))),
        ("compose.override.yaml", "present (auto-included)" if (HARNESS_DIR / "compose.override.yaml").is_file() else "absent"),
        ("ports", f"quay={cfg['QUAY_HOST_PORT']} keycloak={cfg['KEYCLOAK_HOST_PORT']} "
                  f"mailpit={cfg['MAILPIT_HOST_PORT']} jaeger={cfg['JAEGER_HOST_PORT']} "
                  f"garage={cfg['GARAGE_HOST_PORT']}"),
        ("OCP namespace / registry", f"{cfg['OCP_NAMESPACE']} / {cfg['OCP_QUAYREGISTRY_NAME']}"),
    ]
    version = try_run([cfg["DOCKER"], "compose", "version", "--short"])
    rows.insert(6, ("compose version", version.stdout.strip() if version.returncode == 0 else "unknown"))
    for key, val in rows:
        print(f"  {key:<28} {val}")
    if source_mode(cfg) and quay_root and not (quay_root / "web" / "playwright").is_dir():
        print("\nWARNING: QUAY_REPO_ROOT does not look like a quay checkout (no web/playwright).")
        return 1
    return 0


# ---------------------------------------------------------------------------
# OpenShift variant
# ---------------------------------------------------------------------------

VERIFY_BEGIN = "-----BEGIN VERIFY.MD-----"
VERIFY_END = "-----END VERIFY.MD-----"


def oc(cfg: dict, *args, **kw) -> subprocess.CompletedProcess:
    return sh(["oc", "-n", cfg["OCP_NAMESPACE"], *[str(a) for a in args]], **kw)


def oc_quiet(cfg: dict, *args) -> subprocess.CompletedProcess:
    return try_run(["oc", "-n", cfg["OCP_NAMESPACE"], *[str(a) for a in args]])


REALM_IMPORT_HEADER = [
    "# Keycloak realm for the Quay OIDC harness, imported by the Red Hat",
    "# build of Keycloak operator (KeycloakRealmImport) so a bare",
    "# `oc apply -k ocp/` sets up the full IdP from a fresh clone.",
    "#",
    "# Single source of truth: keycloak/quay-realm.json in the harness root.",
    "# run.py refreshes THIS file from that fixture on every render/apply",
    "# (write_generated_manifests) — edit the fixture, never this file; if the",
    "# two drift, the next `./run.sh ocp apply` rewrites this copy and the",
    "# diff shows up in git. The indented JSON under `realm:` is that",
    "# fixture, verbatim (JSON is valid YAML).",
    "#",
    "# Note: the operator only imports realms that do not exist yet —",
    "# re-applying does NOT update an already-imported realm. To force a",
    "# re-import: drop the realm in Keycloak, then",
    "# `oc -n quay-oidc-harness delete keycloakrealmimport quay-oidc-realm`",
    "# and re-apply.",
    "#",
    "# Dev-only fixture credentials (password: \"password\" for every user):",
    "# intentional, see the repo AGENTS.md security note.",
]


def write_generated_manifests(cfg: dict, quay_base_url: str | None = None) -> None:
    """Materialize the OCP manifests that embed harness sources.

    Kustomize's load restrictor forbids referencing files above ocp/, so the
    verify code and the job parameters are embedded into ConfigMap manifests
    here, freshly on every render/apply. They land in ocp/generated/
    (gitignored) and are applied by `ocp verify` only — a bare
    `oc apply -k ocp/` does not need them.

    The KeycloakRealmImport is different: it is TRACKED at
    ocp/idp/quay-oidc-realm.yaml so `oc apply -k ocp/` works from a fresh
    clone. It is refreshed here from keycloak/quay-realm.json, which stays
    the single source of truth.
    """
    OCP_GENERATED_DIR.mkdir(parents=True, exist_ok=True)

    def emit_configmap(name: str, data: dict[str, str], *, path=None,
                       namespace=None, header=()) -> None:
        lines = [*header, "apiVersion: v1", "kind: ConfigMap", "metadata:",
                 f"  name: {name}"]
        if namespace:
            lines.append(f"  namespace: {namespace}")
        lines.append("data:")
        for key, content in data.items():
            lines.append(f"  {key}: |")
            lines.extend(f"    {ln}" if ln else "" for ln in content.splitlines())
        target = path or (OCP_GENERATED_DIR / f"{name}.yaml")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n")

    # KeycloakRealmImport CR embedding the realm fixture under spec.realm
    # (consumed by the Red Hat build of Keycloak operator).
    realm_src = (HARNESS_DIR / "keycloak" / "quay-realm.json").read_text()
    realm_path = OCP_DIR / "idp" / "quay-oidc-realm.yaml"
    realm_lines = [*REALM_IMPORT_HEADER,
                   "apiVersion: k8s.keycloak.org/v2beta1",
                   "kind: KeycloakRealmImport",
                   "metadata:",
                   "  name: quay-oidc-realm",
                   f"  namespace: {cfg['OCP_NAMESPACE']}",
                   "spec:",
                   "  keycloakCRName: quay-keycloak",
                   "  realm:"]
    realm_lines.extend(f"    {ln}" if ln else "" for ln in realm_src.splitlines())
    realm_path.write_text("\n".join(realm_lines) + "\n")

    verify_src = (HARNESS_DIR / "verify.py").read_text()
    emit_configmap("quay-harness-verify", {"verify.py": verify_src},
                   namespace=cfg["OCP_NAMESPACE"])
    emit_configmap("quay-harness-params", {
        "QUAY_BASE_URL": quay_base_url or "",
        "K8S_NAMESPACE": cfg["OCP_NAMESPACE"],
        "QUAY_POD_LABEL": cfg["QUAY_POD_LABEL"],
        "KEYCLOAK_ISSUER": f"http://quay-keycloak.{cfg['OCP_NAMESPACE']}.svc.cluster.local:8080/realms/quay",
        "KEYCLOAK_URL": f"http://quay-keycloak.{cfg['OCP_NAMESPACE']}.svc.cluster.local:8080/realms/quay",
    }, namespace=cfg["OCP_NAMESPACE"])


def render_kustomize(cfg: dict) -> str:
    write_generated_manifests(cfg)
    proc = oc_quiet(cfg, "kustomize", str(OCP_DIR))
    if proc.returncode != 0:
        sys.exit(f"ERROR: oc kustomize failed:\n{proc.stderr}")
    return proc.stdout


def cmd_ocp(cfg: dict, args: list[str]) -> int:
    if not args:
        args = ["help"]
    sub, rest = args[0], args[1:]

    if sub == "render":
        print(render_kustomize(cfg))
        return 0

    if sub == "apply":
        write_generated_manifests(cfg)
        oc(cfg, "apply", "-k", str(OCP_DIR))
        name = cfg["OCP_QUAYREGISTRY_NAME"]
        print(f"Waiting for QuayRegistry {name} to become Available "
              f"(the operator deploys managed components; ~10-15 min) ...")
        deadline = time.time() + 15 * 60
        while time.time() < deadline:
            proc = oc_quiet(cfg, "get", "quayregistry", name, "-o",
                            "jsonpath={.status.conditions[?(@.type=='Available')].status}")
            if proc.stdout.strip() == "True":
                print("QuayRegistry is Available.")
                host = oc_quiet(cfg, "get", "quayregistry", name, "-o",
                                "jsonpath={.status.registryHostname}").stdout.strip()
                print(f"Quay Route: https://{host}")
                print("Next: ./run.sh ocp verify")
                return 0
            time.sleep(15)
        sys.exit("ERROR: QuayRegistry did not become Available. Check: ./run.sh ocp logs")

    if sub == "verify":
        name = cfg["OCP_QUAYREGISTRY_NAME"]
        host = oc_quiet(cfg, "get", "quayregistry", name, "-o",
                        "jsonpath={.status.registryHostname}").stdout.strip()
        if not host:
            sys.exit(f"ERROR: QuayRegistry {name} has no registryHostname yet; run ocp apply first.")
        base_url = f"https://{host}"
        write_generated_manifests(cfg, quay_base_url=base_url)
        # Re-apply the generated ConfigMaps: quay-harness-params now carries
        # the resolved Route URL (it was empty during `ocp apply`).
        for cname in ("quay-harness-verify", "quay-harness-params"):
            proc = subprocess.run(
                ["oc", "apply", "-f", str(OCP_GENERATED_DIR / f"{cname}.yaml")],
                capture_output=True, text=True)
            sys.stdout.write(proc.stdout)
            if proc.returncode != 0:
                sys.exit(f"ERROR: applying {cname} ConfigMap failed:\n{proc.stderr}")

        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        report_dir = REPORTS_DIR / "runs" / f"{stamp}-ocp"
        report_dir.mkdir(parents=True, exist_ok=True)
        latest = REPORTS_DIR / "latest"
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(report_dir)
        print(f"Reports for this run: {report_dir.relative_to(HARNESS_DIR)}")

        job_tmpl = (OCP_DIR / "verify-job.yaml").read_text()
        job_name = f"quay-harness-verify-{stamp}"
        # The template's metadata name is the distinctive quay-harness-verify-job
        # so this replace cannot touch the quay-harness-verify ConfigMap refs.
        job_yaml = job_tmpl.replace("name: quay-harness-verify-job", f"name: {job_name}")
        proc = subprocess.run(["oc", "apply", "-f", "-"], input=job_yaml, text=True,
                              capture_output=True)
        sys.stdout.write(proc.stdout)
        if proc.returncode != 0:
            sys.exit(f"ERROR: applying verify Job failed:\n{proc.stderr}")

        print(f"Waiting for verify Job {job_name} ...")
        deadline = time.time() + 20 * 60
        succeeded = failed = None
        while time.time() < deadline:
            status = oc_quiet(cfg, "get", "job", job_name, "-o", "jsonpath="
                              "{.status.succeeded},{.status.failed}").stdout.strip()
            parts = status.split(",")
            succeeded = parts[0] if parts[0] else None
            failed = parts[1] if len(parts) > 1 and parts[1] else None
            if succeeded or failed:
                break
            time.sleep(10)
        log = oc_quiet(cfg, "logs", f"job/{job_name}")
        (report_dir / "logs").mkdir(exist_ok=True)
        (report_dir / "logs" / "verify-job.log").write_text(log.stdout or "")
        sys.stdout.write(log.stdout or "")

        text = log.stdout or ""
        if VERIFY_BEGIN in text and VERIFY_END in text:
            md = text.split(VERIFY_BEGIN, 1)[1].split(VERIFY_END, 1)[0].strip("\n")
            (report_dir / "verify.md").write_text(md + "\n")
            print(f"Report:  {report_dir.relative_to(HARNESS_DIR)}/verify.md")
        return 0 if succeeded == "1" else 1

    if sub == "logs":
        oc(cfg, "logs", "-f", cfg["QUAY_DEPLOYMENT"], *rest)
        return 0

    if sub == "delete":
        oc(cfg, "delete", "-k", str(OCP_DIR), "--ignore-not-found")
        # The verify/params ConfigMaps are not part of the kustomization
        # (only `ocp verify` applies them); delete them explicitly.
        oc(cfg, "delete", "configmap", "quay-harness-verify", "quay-harness-params",
           "--ignore-not-found")
        return 0

    print(f"Unknown ocp subcommand: {sub}\n\nocp render|apply|verify|logs|delete")
    return 2


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("command", nargs="?", default="help")
    parser.add_argument("args", nargs=argparse.REMAINDER)
    ns = parser.parse_args()

    cfg = load_config()
    cfg["DOCKER_USER"] = detect_docker_user(cfg)
    export_env(cfg)

    cmd, args = ns.command, ns.args
    if cmd in ("help", "-h", "--help"):
        print(USAGE)
        return 0
    handlers = {
        "build": cmd_build, "fetch": cmd_fetch, "up": cmd_up, "verify": cmd_verify,
        "test": cmd_test, "report": cmd_report, "shell": cmd_shell, "logs": cmd_logs,
        "down": cmd_down, "clean": cmd_clean, "doctor": cmd_doctor, "ocp": cmd_ocp,
    }
    handler = handlers.get(cmd)
    if handler is None:
        print(f"Unknown command: {cmd}\n", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2
    return handler(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
