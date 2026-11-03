#!/usr/bin/env python3
"""EDIT-ME product — harness orchestrator.

Stdlib-only on purpose: this file also runs inside the OCP verify Job
(python:3.12-slim, zero pip). Distilled from harnesses/quay-ldap; see
TEMPLATE.md for the contract this skeleton implements.

Layout of this file:
  1. configuration       (DEFAULTS <- .env <- environment)
  2. process/http helpers
  3. compose plumbing    (env-switchable image source, profile "test")
  4. report archiving    (reports/runs/<ts>[-label]/ + reports/latest)
  5. commands            (build up verify test shell logs down clean doctor)
  6. ocp subcommands     (render apply verify logs delete)
"""

from __future__ import annotations

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

# EDIT-ME product: rename PRODUCT_ keys to your product's prefix and fill
# in the defaults. QUAY_* keys below are placeholders.
DEFAULTS = {
    "DOCKER": "docker",
    "PRODUCT_IMAGE": "registry.example/product:latest",  # EDIT-ME pull-mode ref
    "PRODUCT_BUILT_IMAGE": "localhost/product-harness-product:latest",
    "PRODUCT_REPO_ROOT": "",  # set = source mode (build instead of pull)
    "PRODUCT_HOST_PORT": "18080",  # EDIT-ME host port for the product UI/API
    "DOCKER_USER": "",  # "" = auto-detect (rootless -> 0:0)
    "RUN_LABEL": "",
    "REPORT_DIR": "",
    # OpenShift variant
    "OCP_NAMESPACE": "product-harness",  # EDIT-ME
    "OCP_CR_NAME": "product",  # EDIT-ME: operator CR name, if any
    "PRODUCT_POD_LABEL": "app=product",  # EDIT-ME: label selecting product pods
}

OCP_DIR = HARNESS_DIR / "ocp"
OCP_GENERATED_DIR = OCP_DIR / "generated"

USAGE = """\
Usage: ./run.sh <command> [args...]

Commands:
  build               Source mode: build product + runner from PRODUCT_REPO_ROOT.
                      Pull mode: pull PRODUCT_IMAGE (runner needs the repo root).
  up                  Start the stack and wait for readiness.
  verify              Run the verify gate (verify.py) and archive verify.md.
  test [args...]      Run the test suite in the runner container.
  shell / logs / down / clean / doctor
  ocp <render|apply|verify|logs|delete>   OpenShift variant
  help

Environment (optionally via .env): see .env.example.
"""


# ---------------------------------------------------------------------------
# 1. Configuration
# ---------------------------------------------------------------------------

def load_config() -> dict:
    cfg = dict(DEFAULTS)
    env_file = HARNESS_DIR / ".env"
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            cfg[key.strip()] = val.strip().strip('"').strip("'")
    for key in DEFAULTS:
        if os.environ.get(key, "") != "":
            cfg[key] = os.environ[key]
    return cfg


def source_mode(cfg: dict) -> bool:
    """True when the product image should be built from PRODUCT_REPO_ROOT."""
    return bool(cfg["PRODUCT_REPO_ROOT"])


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
# 2. Process / HTTP helpers
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
    """Rootless Docker/Podman: host uid is outside the container's subuid
    range, so `user:` must be 0:0 (container-uid 0 already maps to the host
    user). Rootful engines: the real uid:gid."""
    if cfg["DOCKER_USER"]:
        return cfg["DOCKER_USER"]
    info = try_run([cfg["DOCKER"], "info", "--format", "{{.SecurityOptions}}"])
    if "rootless" in (info.stdout or ""):
        return "0:0"
    return f"{os.getuid()}:{os.getgid()}"


# ---------------------------------------------------------------------------
# 3. Compose plumbing
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
# 4. Report archiving
# ---------------------------------------------------------------------------

def sanitize_label(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-")


def new_report_dir(cfg: dict) -> pathlib.Path:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    label = sanitize_label(cfg["RUN_LABEL"])
    root = pathlib.Path(cfg["REPORT_DIR"]) if cfg["REPORT_DIR"] else HARNESS_DIR / "reports"
    path = root / "runs" / (f"{stamp}-{label}" if label else stamp)
    path.mkdir(parents=True, exist_ok=True)
    latest = root / "latest"
    tmp = latest.with_name("latest.tmp")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(path.resolve())
    tmp.replace(latest)
    print(f"Reports for this run: {path}")
    return path


def check_repo_root(cfg: dict, *, for_what: str) -> pathlib.Path:
    root = pathlib.Path(cfg["PRODUCT_REPO_ROOT"]).expanduser()
    if not (root / "EDIT-ME-suite-marker").exists():  # EDIT-ME: e.g. web/playwright
        sys.exit(
            f"ERROR: {for_what} needs PRODUCT_REPO_ROOT pointing at a product "
            f"source checkout (bind-mounts the test suite). Got: {root!r}"
        )
    return root


# ---------------------------------------------------------------------------
# 5. Commands
# ---------------------------------------------------------------------------

def cmd_build(cfg: dict, args: list[str]) -> int:
    export_env(cfg)
    compose(cfg, "build")
    if not source_mode(cfg):
        compose(cfg, "pull", "product")  # EDIT-ME service name(s)
    return 0


def cmd_up(cfg: dict, args: list[str]) -> int:
    export_env(cfg)
    # EDIT-ME choreography: start dependencies first, wait for each, then
    # the product. Keep the original harness's order literal — it usually
    # encodes hard-won migration/bootstrap knowledge.
    compose(cfg, "up", "-d", "EDIT-ME-deps")
    wait_http(f"http://localhost:{cfg['PRODUCT_HOST_PORT']}", "product",
              tries=120, delay=5, hint="compose logs product")
    return 0


def cmd_verify(cfg: dict, args: list[str]) -> int:
    export_env(cfg)
    report = new_report_dir(cfg)
    env = {**os.environ, "PRODUCT_BASE_URL": f"http://localhost:{cfg['PRODUCT_HOST_PORT']}"}
    proc = sh([sys.executable, str(HARNESS_DIR / "verify.py"),
               "--report-dir", str(report), *args], env=env)
    return proc.returncode


def cmd_test(cfg: dict, args: list[str]) -> int:
    export_env(cfg)
    check_repo_root(cfg, for_what="test")
    report = new_report_dir(cfg)
    logs = report / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    with open(logs / "test.log", "wb") as logf:
        proc = subprocess.run(
            compose_base(cfg) + ["--profile", "test", "run", "--rm", "playwright", *args],
            stdout=logf, stderr=subprocess.STDOUT)
    return proc.returncode


def cmd_shell(cfg: dict, args: list[str]) -> int:
    export_env(cfg)
    compose(cfg, "run", "--rm", "playwright", "bash", *args)
    return 0


def cmd_logs(cfg: dict, args: list[str]) -> int:
    export_env(cfg)
    compose(cfg, "logs", "-f", *(args or []))
    return 0


def cmd_down(cfg: dict, args: list[str]) -> int:
    export_env(cfg)
    compose(cfg, "down", *args)
    return 0


def cmd_clean(cfg: dict, args: list[str]) -> int:
    export_env(cfg)
    compose(cfg, "down", "-v")
    shutil.rmtree(HARNESS_DIR / "reports", ignore_errors=True)
    return 0


def cmd_doctor(cfg: dict, args: list[str]) -> int:
    root = pathlib.Path(cfg["PRODUCT_REPO_ROOT"]).expanduser() if cfg["PRODUCT_REPO_ROOT"] else None
    print("harness        :", HARNESS_DIR.name)
    print("mode           :", "source" if source_mode(cfg) else "pull")
    print("product image  :", cfg["PRODUCT_BUILT_IMAGE"] if source_mode(cfg) else cfg["PRODUCT_IMAGE"])
    print("repo root      :", root if root else "(unset — up/verify only)")
    if root:
        print("suite present  :", (root / "EDIT-ME-suite-marker").exists())
    print("docker user    :", detect_docker_user(cfg))
    print("compose files  :", ", ".join(compose_files(cfg)))
    return 0


# ---------------------------------------------------------------------------
# 6. OpenShift variant
# ---------------------------------------------------------------------------

VERIFY_BEGIN = "-----BEGIN VERIFY.MD-----"
VERIFY_END = "-----END VERIFY.MD-----"


def oc(cfg: dict, *args, **kw) -> subprocess.CompletedProcess:
    return sh(["oc", *args], **kw)


def oc_quiet(cfg: dict, *args) -> subprocess.CompletedProcess:
    return try_run(["oc", *args])


def emit_configmap(path: pathlib.Path, name: str, key_files: dict[str, pathlib.Path]) -> None:
    """Materialize a ConfigMap manifest from single-source harness files.

    EDIT-ME generated: inline each file's contents under `data:` keys.
    Copy the working implementation from harnesses/quay-ldap/run.py
    (write_generated_manifests) — it handles the escaping and the
    kustomize load-restrictor constraint that forces generated files to
    live under ocp/generated/.
    """
    raise NotImplementedError("port write_generated_manifests from quay-ldap")


def write_generated_manifests(cfg: dict, product_base_url: str | None = None) -> None:
    """Materialize ocp/generated/*.yaml from single-source harness files.
    EDIT-ME generated: verify.py, fixtures, params ConfigMap. Called by every
    ocp subcommand so the generated dir can never go stale."""
    OCP_GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    # EDIT-ME: emit_configmap(OCP_GENERATED_DIR / "...", "name", {...})


def render_kustomize(cfg: dict) -> str:
    proc = oc_quiet(cfg, "kustomize", str(OCP_DIR))
    if proc.returncode != 0:
        sys.exit(f"ERROR: oc kustomize failed:\n{proc.stderr}")
    return proc.stdout


def cmd_ocp(cfg: dict, args: list[str]) -> int:
    if not args or args[0] not in ("render", "apply", "verify", "logs", "delete"):
        sys.exit("Usage: ./run.sh ocp <render|apply|verify|logs|delete>")
    sub = args[0]
    export_env(cfg)
    ns = cfg["OCP_NAMESPACE"]

    if sub == "render":
        write_generated_manifests(cfg)
        print(render_kustomize(cfg))
        return 0

    if sub == "apply":
        write_generated_manifests(cfg)
        oc(cfg, "apply", "-k", str(OCP_DIR))
        # EDIT-ME: if the operator exposes an Available condition on a CR,
        # poll it here with a bounded loop instead of a blind oc wait
        # (see quay-ldap run.py cmd_ocp).
        return 0

    if sub == "verify":
        # Resolve the externally reachable URL, then run the SAME verify.py
        # in a Job (ocp/verify-job.yaml) with --log-source ocp.
        # EDIT-ME: quay resolves it from the QuayRegistry status; adapt to
        # your CR / Route.
        write_generated_manifests(cfg, product_base_url=None)
        raise NotImplementedError("port the verify-Job flow from quay-ldap")

    if sub == "logs":
        deployment = cfg.get("PRODUCT_DEPLOYMENT", "EDIT-ME-deployment")
        oc(cfg, "logs", "-f", f"deployment/{deployment}", "-n", ns)
        return 0

    if sub == "delete":
        oc(cfg, "delete", "-k", str(OCP_DIR), "--ignore-not-found")
        return 0
    return 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

COMMANDS = {
    "build": cmd_build, "up": cmd_up, "verify": cmd_verify, "test": cmd_test,
    "shell": cmd_shell, "logs": cmd_logs, "down": cmd_down, "clean": cmd_clean,
    "doctor": cmd_doctor, "ocp": cmd_ocp,
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in ("help", "-h", "--help"):
        print(USAGE)
        return 0
    cmd, args = sys.argv[1], sys.argv[2:]
    handler = COMMANDS.get(cmd)
    if handler is None:
        print(f"ERROR: unknown command {cmd!r}\n\n{USAGE}", file=sys.stderr)
        return 2
    return handler(load_config(), args)


if __name__ == "__main__":
    sys.exit(main())
