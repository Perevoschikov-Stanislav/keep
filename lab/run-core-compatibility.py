"""Snapshot Enterprise files read-only and verify compatibility in an isolated k3d Job."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
CONTEXT, NAMESPACE = "k3d-local", "keep-lab"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True, help="Read-only IaC checkout containing the keep chart")
    parser.add_argument("--example-dir", type=Path, help="Matching local compatibility fixtures; read-only")
    parser.add_argument("--image", default="keep-core-compat-check:local")
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()
    example = args.example_dir or Path(os.environ.get("KEEP_LOCAL_CONFIG_ROOT", ROOT / ".lab-work/config")) / "incident-legacy.example"
    if not example.is_dir() and args.example_dir is None:
        example = ROOT / "config/incident-legacy.example"
    if not (example / "source-manifest.json").is_file():
        parser.error("--example-dir must contain matching fixtures and source-manifest.json")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run = ROOT / ".lab-work/core-compatibility" / stamp
    run.mkdir(parents=True, mode=0o700)
    (run / "tmp").mkdir()
    env = dict(os.environ, TMPDIR=str(run / "tmp"), TMP=str(run / "tmp"), TEMP=str(run / "tmp"), PYTHONDONTWRITEBYTECODE="1")
    kubectl = ["kubectl", "--context", CONTEXT, "--cache-dir=" + str(ROOT / ".lab-work/kube-cache"), "--request-timeout=15s"]

    def command(arguments, log=None, check=True, cwd=ROOT):
        result = subprocess.run(arguments, cwd=cwd, env=env, capture_output=True, text=True)
        if log:
            (run / log).write_text(result.stdout + result.stderr)
        if check and result.returncode:
            raise RuntimeError(f"Command failed; see {run / (log or 'result.json')}")
        return result

    config = json.loads(command(kubectl + ["config", "view", "--minify", "-o", "json"]).stdout)
    if urlsplit(config["clusters"][0]["cluster"]["server"]).hostname not in {"127.0.0.1", "localhost", "0.0.0.0", "::1"}:
        raise RuntimeError("k3d-local must point to a loopback API")
    source = args.source_root.resolve()
    before = command(["git", "status", "--porcelain"], cwd=source).stdout
    manifest = {"checked_at": datetime.now(timezone.utc).isoformat(), "source_root": str(source),
        "source_head": command(["git", "rev-parse", "HEAD"], cwd=source).stdout.strip(), "files": {}}
    paths = [source / name for name in ["keep/bridge/bridge.py", "keep/bridge/test_bridge.py", "keep/config/keepconf.py",
                                      "keep/config/extraction.yaml", "keep/config/rules/correlation.yaml"]]
    for directory in ("mapping", "workflows"):
        paths.extend(sorted((source / "keep/config" / directory).glob("*.yaml")))
    data, items = {}, []

    def add(relative, content):
        key = f"source-{len(data):03d}"
        data[key] = content
        items.append({"key": key, "path": relative})

    for path in paths:
        relative = path.relative_to(source)
        content = path.read_text()
        manifest["files"][str(relative)] = hashlib.sha256(content.encode()).hexdigest()
        local = run / "source" / relative
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text(content)
        local.chmod(0o600)
        add("source/" + str(relative), content)
    code = json.loads(command(kubectl + ["-n", NAMESPACE, "get", "cm", "keep-mm-bridge-code", "-o", "json"]).stdout)["data"]["bridge.py"]
    manifest["lab_bridge_sha256"] = hashlib.sha256(code.encode()).hexdigest()
    for path in [ROOT / "lab/check-core-compatibility.py", ROOT / "tests/team_fork_test_case.py", ROOT / "tests/__init__.py",
                 ROOT / "docs/fork/incident-core/fixtures-v1.json"]:
        add(str(path.relative_to(ROOT)), path.read_text())
    for path in sorted(example.rglob("*.yaml")) + [example / "source-manifest.json"]:
        add("config/incident-legacy.example/" + str(path.relative_to(example)), path.read_text())
    (run / "source-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    if not args.skip_build:
        print("Build compatibility image with the current fork source", flush=True)
        command(["docker", "build", "-f", "lab/Dockerfile.core-compat-check", "-t", args.image, "."], "build.log")
    command(["k3d", "image", "import", args.image, "-c", "local"], "import.log")
    name = "keep-core-compat-" + stamp.lower()
    labels = {"app.kubernetes.io/name": "keep-core-compatibility", "keep.compatibility-run": name}
    mounts = [{"name": "source", "mountPath": "/contract", "readOnly": True}, {"name": "work", "mountPath": "/work"}]
    values = {"PYTHONPATH": "/silences-check/deps:/contract:/app", "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": "/work/tmp", "TMP": "/work/tmp", "TEMP": "/work/tmp", "DATABASE_CONNECTION_STRING": "sqlite://",
        "KEEP_STORAGE_DIRECTORY": "/work/storage", "PROMETHEUS_MULTIPROC_DIR": "/work/prometheus", "AUTH_TYPE": "OAUTH2PROXY",
        "KEEP_OSS_ONLY": "true", "POSTHOG_DISABLED": "true", "SENTRY_DISABLED": "true", "KEEP_IMPERSONATION_ENABLED": "true",
        "KEEP_OAUTH2_PROXY_USER_HEADER": "x-forwarded-email", "KEEP_OAUTH2_PROXY_ROLE_HEADER": "x-forwarded-groups",
        "KEEP_COMPAT_SOURCE_ROOT": "/contract/source/keep",
        "KEEP_COMPAT_TEST_DATABASE": "postgresql+psycopg://postgres@127.0.0.1:5432/core_compatibility"}
    security = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}
    job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "namespace": NAMESPACE, "labels": labels},
        "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 300, "template": {"metadata": {"labels": labels}, "spec": {
            "automountServiceAccountToken": False, "restartPolicy": "Never",
            "securityContext": {"runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000},
            "containers": [{"name": "verify", "image": args.image, "imagePullPolicy": "Never", "workingDir": "/contract",
                "command": ["/venv/bin/python", "lab/check-core-compatibility.py"],
                "env": [{"name": k, "value": v} for k, v in values.items()], "volumeMounts": mounts, "securityContext": security,
                "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"cpu": "1", "memory": "768Mi"}}}],
            "initContainers": [{"name": "postgres", "restartPolicy": "Always", "image": "postgres:15-alpine",
                "command": ["sh", "-c", "mkdir -p /pgdata/tmp && exec docker-entrypoint.sh postgres -c listen_addresses=127.0.0.1 -c shared_buffers=16MB -c max_connections=20"],
                "env": [{"name": k, "value": v} for k, v in {"POSTGRES_HOST_AUTH_METHOD": "trust", "POSTGRES_DB": "core_compatibility",
                    "PGDATA": "/pgdata/data", "TMPDIR": "/pgdata/tmp", "TMP": "/pgdata/tmp", "TEMP": "/pgdata/tmp"}.items()],
                "startupProbe": {"exec": {"command": ["pg_isready", "-h", "127.0.0.1", "-U", "postgres"]}, "periodSeconds": 1, "failureThreshold": 60},
                "securityContext": {**security, "runAsUser": 70, "runAsGroup": 70},
                "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}, "limits": {"cpu": "1", "memory": "256Mi"}},
                "volumeMounts": [{"name": "pgdata", "mountPath": "/pgdata"}, {"name": "pgsocket", "mountPath": "/var/run/postgresql"}]}],
            "volumes": [{"name": "source", "configMap": {"name": name, "items": items}},
                {"name": "work", "emptyDir": {"sizeLimit": "64Mi"}}, {"name": "pgdata", "emptyDir": {"sizeLimit": "512Mi"}},
                {"name": "pgsocket", "emptyDir": {"sizeLimit": "16Mi"}}]}}}}
    configmap = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, "namespace": NAMESPACE, "labels": labels}, "immutable": True, "data": data}
    policy = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": name, "namespace": NAMESPACE},
        "spec": {"podSelector": {"matchLabels": {"keep.compatibility-run": name}}, "policyTypes": ["Ingress", "Egress"], "ingress": [], "egress": []}}
    result = {"context": CONTEXT, "namespace": NAMESPACE, "job": name, "image": args.image, "passed": False,
        "source_head": manifest["source_head"], "core_unchanged": False, "run": str(run)}
    (run.parent / "CURRENT").write_text(str(run) + "\n")
    for filename, document in [("network-policy.json", policy), ("configmap.json", configmap), ("job.json", job)]:
        path = run / filename
        path.write_text(json.dumps(document, indent=2) + "\n")
        print(f"kubectl --context {CONTEXT} -n {NAMESPACE} apply -f {path}", flush=True)
        command(kubectl + ["apply", "-f", str(path)], filename + ".log")
    deadline = time.monotonic() + 330
    while time.monotonic() < deadline:
        status = json.loads(command(kubectl + ["-n", NAMESPACE, "get", "job", name, "-o", "json"]).stdout)
        condition = next((c for c in status.get("status", {}).get("conditions", []) if c["status"] == "True" and c["type"] in {"Complete", "Failed", "FailureTarget"}), None)
        if condition:
            result["passed"] = condition["type"] == "Complete"
            (run / "job-result.json").write_text(json.dumps(status, indent=2) + "\n")
            break
        time.sleep(2)
    log = command(kubectl + ["-n", NAMESPACE, "logs", "job/" + name, "-c", "verify"], "checks.log", check=False)
    result["core_unchanged"] = command(["git", "status", "--porcelain"], cwd=source).stdout == before and all(
        hashlib.sha256((source / rel).read_bytes()).hexdigest() == digest for rel, digest in manifest["files"].items())
    (run / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(log.stdout[-14000:], flush=True)
    print(json.dumps(result, indent=2), flush=True)
    return 0 if result["passed"] and result["core_unchanged"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
