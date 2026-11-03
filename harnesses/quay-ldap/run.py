#!/usr/bin/env python3
"""Orchestrator for the containerized LDAP harness (PROJQUAY-13372).

Standalone port of the original .cursor/ldap-testing/run.sh, rewritten in
stdlib-only Python so the harness is self-contained and agent-extensible.

Image source:
  * pull mode (default): run ${QUAY_IMAGE}, a published Quay image.
  * source mode: set QUAY_REPO_ROOT to a quay checkout; every compose
    invocation then includes compose.build.yaml, which builds the quay image
    from that repo instead.

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
    "QUAY_BUILT_IMAGE": "localhost/quay-ldap-testing-quay:latest",
    "QUAY_REPO_ROOT": "",
    "QUAY_HOST_PORT": "18090",
    "LDAP_HOST_PORT": "13389",
    "MAILPIT_HOST_PORT": "18026",
    "DOCKER_USER": "",  # "" = auto-detect
    "RUN_LABEL": "",
    "REPORT_DIR": "",
    # OpenShift variant (ocp subcommands)
    "OCP_NAMESPACE": "quay-ldap-harness",
    "OCP_QUAYREGISTRY_NAME": "quay",
    "QUAY_POD_LABEL": "app=quay",
    "QUAY_DEPLOYMENT": "quay-quay-app",  # operator convention: <name>-quay-app
}

OCP_DIR = HARNESS_DIR / "ocp"
OCP_GENERATED_DIR = OCP_DIR / "generated"

USAGE = """\
Usage: ./run.sh <command> [args...]

Commands:
  build               Build images. Source mode: quay (from QUAY_REPO_ROOT)
                      + the Playwright runner. Pull mode: pulls QUAY_IMAGE
                      (the runner cannot be built without QUAY_REPO_ROOT).
  up                  Start the stack (db/redis/mailpit/ldap, then quay) and
                      wait for readiness.
  verify              Exercise the PROJQUAY-13372 fix: trigger LDAP logins
                      (valid, invalid, PBKDF2-hash user), then scan the quay
                      container logs for password leaks and for the required
                      redaction masks. Writes verify.md + logs under
                      reports/runs/<timestamp>[-label]/.
  test [args...]      Run the Playwright suite in the runner container.
                      With no args: the @auth:LDAP specs. Extra args go to
                      `npx playwright test`, e.g.:
                        ./run.sh test --grep "@critical"
  shell               Open a bash shell in the runner container.
  logs [service]      Follow compose logs.
  down                Stop containers, keep volumes and reports.
  clean               down -v and remove generated reports.
  doctor              Print the resolved configuration and environment checks.
  ocp <subcommand>    OpenShift variant (operator-installed Quay):
                        render   print the rendered kustomize manifests
                        apply    deploy namespace + IdP + QuayRegistry, wait
                                 for the registry to become Available
                        verify   run the verify Job against the Quay Route
                        logs     follow the quay-app deployment logs
                        delete   remove everything the harness created
  help                Show this help.

Environment (optionally via .env, see .env.example): DOCKER, QUAY_IMAGE,
QUAY_REPO_ROOT, QUAY_BUILT_IMAGE, QUAY_HOST_PORT, LDAP_HOST_PORT,
MAILPIT_HOST_PORT, DOCKER_USER, REPORT_DIR, RUN_LABEL, OCP_NAMESPACE,
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
            # Strip surrounding quotes; strip trailing comments on unquoted values.
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
    """True when the quay image should be built from QUAY_REPO_ROOT."""
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
    """Export config so compose variable interpolation and children see it."""
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


def use_aliases(cfg: dict) -> list[str]:
    """compose run --use-aliases lets Quay reach the webhook receiver by the
    service name `playwright`; probe for support (older compose lacks it)."""
    help_text = try_run(compose_base(cfg) + ["run", "--help"]).stdout or ""
    return ["--use-aliases"] if "--use-aliases" in help_text else []


# ---------------------------------------------------------------------------
# Compose plumbing
# ---------------------------------------------------------------------------

def compose_base(cfg: dict) -> list[str]:
    return [cfg["DOCKER"], "compose", *compose_files(cfg)]


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
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-")
    return label


def new_report_dir(cfg: dict) -> pathlib.Path:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    label = sanitize_label(cfg["RUN_LABEL"])
    dirname = f"{stamp}-{label}" if label else stamp
    report_dir = REPORTS_DIR / "runs" / dirname
    report_dir.mkdir(parents=True, exist_ok=True)
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
# Commands
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


def cmd_build(cfg: dict, args: list[str]) -> int:
    if source_mode(cfg):
        check_repo_root(cfg, for_what="source-mode build")
        compose(cfg, "build", *args)
    else:
        # Pull mode: nothing to build. The runner cannot be built without a
        # quay checkout (its build context needs web/); warm the image cache.
        print("Pull mode (QUAY_REPO_ROOT unset): pulling QUAY_IMAGE, nothing to build.")
        print("Note: `test` needs QUAY_REPO_ROOT even in pull mode (the suite")
        print("lives in the quay checkout's web/playwright).")
        compose(cfg, "pull", "quay")
    return 0


def wait_for_ldap(cfg: dict) -> None:
    print("Waiting for the 389 DS LDAP server (first boot imports the LDIF, ~40s) ...")
    for _ in range(60):
        probe = compose_quiet(cfg, "exec", "-T", "quay-ldap",
                              "ldapsearch", "-x", "-H", "ldap://localhost:3389",
                              "-b", "dc=example,dc=org", "-s", "base")
        if probe.returncode == 0:
            print("LDAP server is ready.")
            return
        time.sleep(5)
    sys.exit("ERROR: LDAP server did not become ready. Check: ./run.sh logs quay-ldap")


def cmd_up(cfg: dict, args: list[str]) -> int:
    if args:
        # Direct passthrough for power users.
        compose(cfg, "up", *args)
        return 0
    compose(cfg, "up", "-d", "quay-db", "quay-redis", "quay-mailpit", "quay-ldap",
            profiles=False)
    wait_for_ldap(cfg)
    compose(cfg, "up", "-d", "quay", profiles=False)
    wait_http(f"http://127.0.0.1:{cfg['QUAY_HOST_PORT']}/health/instance",
              "Quay", tries=120, delay=5, hint="First boot runs migrations (start_period 300s).")
    print(f"Quay:      http://localhost:{cfg['QUAY_HOST_PORT']}")
    print(f"LDAP:      ldap://localhost:{cfg['LDAP_HOST_PORT']} (Directory Manager / admin)")
    print(f"Mailpit:   http://localhost:{cfg['MAILPIT_HOST_PORT']}")
    print("Mode:      " + ("source (built from QUAY_REPO_ROOT)" if source_mode(cfg) else f"pull ({cfg['QUAY_IMAGE']})"))
    print("Next:      ./run.sh verify   (PROJQUAY-13372 redaction check)")
    return 0


def cmd_verify(cfg: dict, args: list[str]) -> int:
    report_dir = new_report_dir(cfg)
    sys.path.insert(0, str(HARNESS_DIR))
    import verify  # the harness's own data-driven redaction gate

    argv = [
        "--base-url", f"http://127.0.0.1:{cfg['QUAY_HOST_PORT']}",
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
    return 0


def cmd_doctor(cfg: dict, args: list[str]) -> int:
    cfg["DOCKER_USER"] = detect_docker_user(cfg)
    quay_root = pathlib.Path(cfg["QUAY_REPO_ROOT"]).expanduser() if cfg["QUAY_REPO_ROOT"] else None
    rows = [
        ("mode", "source (build from QUAY_REPO_ROOT)" if source_mode(cfg) else "pull (published image)"),
        ("QUAY_IMAGE", cfg["QUAY_IMAGE"]),
        ("QUAY_BUILT_IMAGE", cfg["QUAY_BUILT_IMAGE"] + "  (source mode)"),
        ("QUAY_REPO_ROOT", cfg["QUAY_REPO_ROOT"] or "<unset>"),
        ("  web/playwright present", "yes" if quay_root and (quay_root / "web" / "playwright").is_dir()
         else ("no" if quay_root else "n/a")),
        ("DOCKER", cfg["DOCKER"]),
        ("DOCKER_USER (runner)", cfg["DOCKER_USER"]),
        ("compose files", ", ".join(compose_files(cfg))),
        ("compose.override.yaml", "present (auto-included)" if (HARNESS_DIR / "compose.override.yaml").is_file() else "absent"),
        ("ports", f"quay={cfg['QUAY_HOST_PORT']} ldap={cfg['LDAP_HOST_PORT']} mailpit={cfg['MAILPIT_HOST_PORT']}"),
        ("OCP namespace / registry", f"{cfg['OCP_NAMESPACE']} / {cfg['OCP_QUAYREGISTRY_NAME']}"),
    ]
    version = try_run([cfg["DOCKER"], "compose", "version", "--short"])
    rows.insert(5, ("compose version", version.stdout.strip() if version.returncode == 0 else "unknown"))
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


def write_generated_manifests(cfg: dict, quay_base_url: str | None = None) -> None:
    """Materialize ocp/generated/ from the harness's single sources of truth.

    Kustomize's load restrictor forbids referencing files above ocp/, so the
    verify code, the LDIF and the job parameters are embedded into generated
    ConfigMap manifests here, freshly on every render/apply.
    """
    OCP_GENERATED_DIR.mkdir(parents=True, exist_ok=True)

    def emit_configmap(name: str, data: dict[str, str]) -> None:
        lines = [f"apiVersion: v1", f"kind: ConfigMap", f"metadata:",
                 f"  name: {name}", f"data:"]
        for key, content in data.items():
            lines.append(f"  {key}: |")
            lines.extend(f"    {ln}" if ln else "" for ln in content.splitlines())
        (OCP_GENERATED_DIR / f"{name}.yaml").write_text("\n".join(lines) + "\n")

    verify_src = (HARNESS_DIR / "verify.py").read_text()
    ldif_src = (HARNESS_DIR / "ldap" / "base.ldif").read_text()
    emit_configmap("quay-harness-verify", {"verify.py": verify_src, "base.ldif": ldif_src})

    # The on-cluster 389 DS mounts its LDIF + init script from this ConfigMap
    # (single-sourced from ldap/ — see ocp/idp/389ds.yaml).
    init_src = (HARNESS_DIR / "ldap" / "init-389ds.sh").read_text()
    emit_configmap("quay-ldap-ldif", {"base.ldif": ldif_src, "init-389ds.sh": init_src})

    params = {
        "QUAY_BASE_URL": quay_base_url or "",
        "K8S_NAMESPACE": cfg["OCP_NAMESPACE"],
        "QUAY_POD_LABEL": cfg["QUAY_POD_LABEL"],
    }
    emit_configmap("quay-harness-params",
                   {k: v for k, v in params.items()})


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
        for name in ("quay-harness-verify", "quay-harness-params"):
            proc = subprocess.run(
                ["oc", "apply", "-f", str(OCP_GENERATED_DIR / f"{name}.yaml")],
                capture_output=True, text=True)
            sys.stdout.write(proc.stdout)
            if proc.returncode != 0:
                sys.exit(f"ERROR: applying {name} ConfigMap failed:\n{proc.stderr}")

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

        # Poll for completion (succeeded or failed) instead of a single
        # condition wait, so a failed check surfaces immediately.
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

        # The Job prints verify.md between marker lines (verify.py --print-report).
        text = log.stdout or ""
        status = oc_quiet(cfg, "get", "job", job_name, "-o",
                          "jsonpath={.status.succeeded}").stdout.strip()
        if VERIFY_BEGIN in text and VERIFY_END in text:
            md = text.split(VERIFY_BEGIN, 1)[1].split(VERIFY_END, 1)[0].strip("\n")
            (report_dir / "verify.md").write_text(md + "\n")
            print(f"Report:  {report_dir.relative_to(HARNESS_DIR)}/verify.md")
        return 0 if status == "1" else 1

    if sub == "logs":
        oc(cfg, "logs", "-f", cfg["QUAY_DEPLOYMENT"], *rest)
        return 0

    if sub == "delete":
        oc(cfg, "delete", "-k", str(OCP_DIR), "--ignore-not-found")
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
        "build": cmd_build, "up": cmd_up, "verify": cmd_verify, "test": cmd_test,
        "shell": cmd_shell, "logs": cmd_logs, "down": cmd_down, "clean": cmd_clean,
        "doctor": cmd_doctor, "ocp": cmd_ocp,
    }
    handler = handlers.get(cmd)
    if handler is None:
        print(f"Unknown command: {cmd}\n", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2
    return handler(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
