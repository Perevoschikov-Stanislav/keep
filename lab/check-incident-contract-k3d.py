"""Verify the policy contract as a k3d Job; save inputs/results inside the fork."""

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
CONTRACT = ROOT / "docs/fork/incident-core"
CONTEXT = "k3d-local"
NAMESPACE = "keep-lab"


def main(suite="incident-contract"):
    core_verification = suite == "incident-core-verification"
    migration = core_verification or suite == "incident-migration"
    notifications = migration or suite == "incident-notifications"
    automation = notifications or suite == "incident-automation"
    lifecycle = automation or suite == "incident-lifecycle"
    correlation = lifecycle or suite == "incident-correlation"
    normalization = correlation or suite == "event-normalization"
    provisioning = normalization or suite == "incident-provisioning"
    integrations = provisioning or suite == "silences-integration"
    task = "31" if core_verification else "30" if migration else "29" if notifications else "28" if automation else "27" if lifecycle else "26" if correlation else "25" if normalization else "24" if provisioning else "20" if integrations else "23"
    entrypoint = "check-incident-core-verification.py" if core_verification else "check-incident-migration.py" if migration else "check-incident-notifications.py" if notifications else "check-incident-automation.py" if automation else "check-incident-lifecycle.py" if lifecycle else "check-incident-correlation.py" if correlation else "check-event-normalization.py" if normalization else "check-incident-provisioning.py" if provisioning else "check-silences-integration.py" if integrations else "check-incident-contract.py"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="keep-core-verification-check:local" if core_verification else "keep-migration-check:local" if migration else "keep-notifications-check:local" if notifications else "keep-automation-check:local" if automation else "keep-lifecycle-check:local" if lifecycle else "keep-correlation-check:local" if correlation else "keep-normalization-check:local" if normalization else "keep-provisioning-check:local" if provisioning else "keep-silences-check:local" if integrations else "keep-contract-check:local", help="Local verification image tag")
    parser.add_argument("--base-image", default="keep-backend:lab-review-20261004", help="Existing backend image used as dependency base")
    parser.add_argument("--skip-build", action="store_true", help="Use an existing verification image")
    parser.add_argument("--skip-import", action="store_true", help="Use an image already imported into this k3d node")
    if automation:
        parser.add_argument("--tests", nargs="+", help="Run selected unittest names for debugging")
        parser.add_argument("--bridge-mattermost", action="store_true", help="Task 21: use the local Mattermost in isolated test channels")
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run = ROOT / (".lab-work/incident-core-verification-k3d" if core_verification else ".lab-work/incident-migration-k3d" if migration else ".lab-work/incident-notifications-k3d" if notifications else ".lab-work/incident-automation-k3d" if automation else ".lab-work/incident-lifecycle-k3d" if lifecycle else ".lab-work/incident-correlation-k3d" if correlation else ".lab-work/event-normalization-k3d" if normalization else ".lab-work/incident-provisioning-k3d" if provisioning else ".lab-work/silences-integration-check" if integrations else ".lab-work/incident-contract") / stamp
    run.mkdir(parents=True)
    (run / "tmp").mkdir()
    env = dict(os.environ, TMPDIR=str(run / "tmp"), TMP=str(run / "tmp"), TEMP=str(run / "tmp"),
               PYTHONDONTWRITEBYTECODE="1")
    kubectl = ["kubectl", "--context", CONTEXT, "--cache-dir=" + str(ROOT / ".lab-work/kube-cache"), "--request-timeout=15s"]

    def command(arguments, log=None, check=True):
        result = subprocess.run(arguments, cwd=ROOT, env=env, capture_output=True, text=True)
        if log:
            (run / log).write_text(result.stdout + result.stderr)
        if check and result.returncode:
            raise RuntimeError(f"Command failed ({arguments[0]}); see {run / (log or 'result.json')}")
        return result

    # Verify that the explicitly named context really is local; never use the default kubectl context.
    config = json.loads(command(kubectl + ["config", "view", "--minify", "-o", "json"]).stdout)
    server = config["clusters"][0]["cluster"]["server"]
    if urlsplit(server).hostname not in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}:
        raise RuntimeError("k3d-local context does not point to a loopback Kubernetes API")
    command(["k3d", "cluster", "list"], "cluster.log")
    if not args.skip_build:
        print(f"Build contract verification image: {args.image}", flush=True)
        command(["docker", "build", "--build-arg", "KEEP_BACKEND_IMAGE=" + args.base_image,
                 "-f", "lab/Dockerfile.silences-check" if integrations else "lab/Dockerfile.contract-check",
                 "-t", args.image, "."], "image-build.log")
    image = json.loads(command(["docker", "image", "inspect", args.image]).stdout)[0]
    if not args.skip_import:
        print(f"Import local image into k3d cluster local: {args.image}", flush=True)
        command(["k3d", "image", "import", args.image, "-c", "local"], "image-import.log")

    paths = [ROOT / "lab/incident_contract.py", ROOT / "lab/check-incident-contract.py",
             ROOT / "config/team-policy.example.yaml", CONTRACT / "contract-v1.schema.json",
             CONTRACT / "parameters-v1.md", CONTRACT / "fixtures-v1.json"]
    paths.extend(sorted((CONTRACT / "examples").rglob("*.yaml")))
    if integrations:
        paths.extend([ROOT / "lab/check-silences-integration.py", ROOT / "lab/migrate-legacy-silences.py"])
        paths.extend([ROOT / "lab/team-policy.yaml", ROOT / "lab/keycloak-memberships.yaml"])
        paths.extend(sorted((ROOT / "tests").glob("*silence*.py")))
        paths.extend(sorted((ROOT / "tests").glob("test_team*_fork.py")))
        paths.extend([ROOT / "tests/team_fork_test_case.py", ROOT / "tests/__init__.py"])
        paths.append(ROOT / "tests/test_oss_request_context_fork.py")
        paths.extend(sorted((ROOT / "keep/api/models/db/migrations/versions").glob("2026-10-*.py")))
    if provisioning:
        paths.extend([ROOT / "lab/check-incident-provisioning.py", ROOT / "tests/test_incident_provisioning_fork.py",
                      ROOT / "tests/test_incident_provisioning_postgres_fork.py"])
        paths.extend(sorted((ROOT / "config/incident-policies.example").rglob("*.yaml")))
    if normalization:
        paths.extend([ROOT / "lab/check-event-normalization.py", ROOT / "tests/test_event_normalization_fork.py",
                      ROOT / "tests/test_event_normalization_postgres_fork.py"])
        paths.extend(sorted((ROOT / "config/event-normalization.example").rglob("*.yaml")))
    if correlation:
        paths.extend([ROOT / "lab/check-incident-correlation.py", ROOT / "tests/test_incident_correlation_fork.py",
                      ROOT / "tests/test_incident_correlation_postgres_fork.py"])
        paths.extend(sorted((ROOT / "config/incident-correlation.example").rglob("*.yaml")))
    if lifecycle:
        paths.extend([ROOT / "lab/check-incident-lifecycle.py", ROOT / "tests/test_incident_lifecycle_fork.py",
                      ROOT / "tests/test_incident_lifecycle_postgres_fork.py"])
        paths.extend(sorted((ROOT / "config/incident-lifecycle.example").rglob("*.yaml")))
    if automation:
        paths.extend([ROOT / "lab/check-incident-automation.py", ROOT / "tests/test_incident_automation_fork.py",
                      ROOT / "tests/test_incident_automation_postgres_fork.py"])
        paths.extend(sorted((ROOT / "config/incident-automation.example").rglob("*.yaml")))
    if notifications:
        paths.extend([ROOT / "transports/mattermost/bridge.py", ROOT / "transports/mattermost/contract.json",
                      ROOT / "transports/mattermost/build-contract.py", ROOT / "keep/api/core/incident_contract_v1.schema.json",
                      ROOT / "docs/fork/silences/contract-v1.schema.json"])
        paths.extend(sorted((ROOT / "config/incident-bridge.example").rglob("*.yaml")))
        paths.extend(sorted((ROOT / "config/incident-bridge.example").rglob("*.json")))
        paths.extend([ROOT / "lab/check-incident-notifications.py", ROOT / "tests/incident_notification_fixtures.py"])
        paths.extend(sorted((ROOT / "tests").glob("test_incident_notifications*_fork.py")))
        paths.extend(sorted((ROOT / "config/incident-notifications.example").rglob("*.yaml")))
        for directory in ("incident-core.lab", "incident-core.target"):
            paths.extend(sorted((ROOT / "config" / directory).rglob("*.yaml")))
            paths.extend(sorted((ROOT / "config" / directory).rglob("*.json")))
    if migration:
        paths.extend([ROOT / "lab/check-incident-migration.py", ROOT / "tests/test_legacy_incident_migration_fork.py",
                      ROOT / "tests/test_legacy_incident_migration_postgres_fork.py"])
        paths.extend(sorted((ROOT / "config/incident-migration.example").rglob("*.yaml")))
    if core_verification:
        paths.extend([ROOT / "lab/check-incident-core-verification.py",
                      ROOT / "tests/test_incident_core_verification_fork.py",
                      ROOT / "tests/test_incident_core_verification_postgres_fork.py",
                      ROOT / "tests/test_alertmanager_reconciliation_fork.py",
                      ROOT / "tests/test_alertmanager_sync_fork.py",
                      ROOT / "tests/test_alertmanager_reconciliation_postgres_fork.py"])
    data, items, manifest = {}, [], {}
    for index, path in enumerate(paths):
        relative = str(path.relative_to(ROOT))
        content = path.read_text()
        key = f"source-{index:03d}"
        data[key] = content
        items.append({"key": key, "path": relative})
        manifest[relative] = hashlib.sha256(content.encode()).hexdigest()
    name = ("keep-core-31-" if core_verification else "keep-migration-30-" if migration else "keep-notifications-29-" if notifications else "keep-automation-28-" if automation else "keep-lifecycle-27-" if lifecycle else "keep-correlation-26-" if correlation else "keep-normalization-25-" if normalization else "keep-iac-24-" if provisioning else "keep-silences-20-" if integrations else "keep-contract-23-") + stamp.lower()
    labels = {"app.kubernetes.io/name": "keep-silences-check" if integrations else "keep-contract-check", "keep.task": task}
    # The growing regression fixtures exceed Kubernetes' 1 MiB ConfigMap cap.
    chunks, current, size = [], {}, 0
    for key, content in data.items():
        length = len(content.encode())
        if current and size + length > 700_000:
            chunks.append(current)
            current, size = {}, 0
        current[key] = content
        size += length
    if current:
        chunks.append(current)
    configmaps = [{"apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": name + "-" + str(index), "namespace": NAMESPACE, "labels": labels},
        "immutable": True, "data": chunk} for index, chunk in enumerate(chunks)]
    configmap = {"apiVersion": "v1", "kind": "List", "items": configmaps}
    job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "namespace": NAMESPACE, "labels": labels},
           "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 1400 if (core_verification or migration) else 600 if correlation else 330 if integrations else 120, "template": {
               "metadata": {"labels": labels}, "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                   "securityContext": {"runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000},
                   "containers": [{"name": "verify", "image": args.image, "imagePullPolicy": "Never",
                       "command": ["/venv/bin/python", "/contract/lab/" + entrypoint], "workingDir": "/contract",
                       "env": [{"name": key, "value": value} for key, value in {
                           "PYTHONPATH": ("/silences-check/deps" if integrations else "/contract-check/deps") + ":/contract:/app", "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": "/work/tmp",
                           "TMP": "/work/tmp", "TEMP": "/work/tmp", "XDG_CACHE_HOME": "/work/cache",
                           "DATABASE_CONNECTION_STRING": "sqlite:////work/unused.db", "KEEP_STORAGE_DIRECTORY": "/work/storage",
                           "PROMETHEUS_MULTIPROC_DIR": "/work/prometheus",
                           "AUTH_TYPE": "OAUTH2PROXY", "KEEP_OSS_ONLY": "true", "POSTHOG_DISABLED": "true", "SENTRY_DISABLED": "true",
                           "KEEP_CONTRACT_COMPATIBILITY": "1",
                           **({"KEEP_CHECK_TESTS": json.dumps(args.tests)} if automation and args.tests else {}),
                           **({"KEEP_INTEGRATION_TEST_DATABASE": "postgresql+psycopg://postgres@127.0.0.1:5432/silences_check"} if integrations else {})}.items()],
                       "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"cpu": "1", "memory": "768Mi"}},
                       "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                           "capabilities": {"drop": ["ALL"]}},
                       "volumeMounts": [{"name": "source", "mountPath": "/contract", "readOnly": True},
                                        {"name": "work", "mountPath": "/work"}]}],
                   "volumes": [{"name": "source", "projected": {"sources": [{"configMap": {
                       "name": cm["metadata"]["name"], "items": [item for item in items if item["key"] in cm["data"]]}}
                       for cm in configmaps]}},
                               {"name": "work", "emptyDir": {"sizeLimit": "64Mi"}}]}}}}
    if integrations:
        pod = job["spec"]["template"]["spec"]
        pod["initContainers"] = [{"name": "postgres", "restartPolicy": "Always", "image": "postgres:15-alpine",
            "imagePullPolicy": "IfNotPresent", "command": ["sh", "-c",
                "mkdir -p /pgdata/tmp && exec docker-entrypoint.sh postgres -c listen_addresses=127.0.0.1 -c shared_buffers=16MB -c max_connections=20 -c max_wal_size=64MB -c min_wal_size=32MB"],
            "env": [{"name": key, "value": value} for key, value in {
                "POSTGRES_HOST_AUTH_METHOD": "trust", "POSTGRES_DB": "silences_check", "PGDATA": "/pgdata/data",
                "TMPDIR": "/pgdata/tmp", "TMP": "/pgdata/tmp", "TEMP": "/pgdata/tmp"}.items()],
            "startupProbe": {"exec": {"command": ["pg_isready", "-h", "127.0.0.1", "-U", "postgres"]},
                             "periodSeconds": 1, "failureThreshold": 60},
            "securityContext": {"runAsUser": 70, "runAsGroup": 70, "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
            "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}, "limits": {"cpu": "1", "memory": "384Mi"}},
            "volumeMounts": [{"name": "pgdata", "mountPath": "/pgdata"},
                             {"name": "pgsocket", "mountPath": "/var/run/postgresql"}]}]
        pod["volumes"].extend([{"name": "pgdata", "emptyDir": {"sizeLimit": "512Mi"}},
                               {"name": "pgsocket", "emptyDir": {"sizeLimit": "16Mi"}}])
    if automation and args.bridge_mattermost:
        deployment = json.loads(command(kubectl + ["-n", NAMESPACE, "get", "deployment", "keep-mm-bridge", "-o", "json"]).stdout)
        bridge_env = {item["name"]: item.get("value") for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
        if urlsplit(bridge_env["MM_URL"]).hostname != "mattermost.keep-lab.svc":
            raise RuntimeError("Task 21 tests permit only the local lab Mattermost")
        job["spec"]["template"]["spec"]["containers"][0]["env"].extend([
            {"name": "KEEP_BRIDGE_TEST_MM_URL", "value": bridge_env["MM_URL"]},
            {"name": "KEEP_BRIDGE_TEST_CHANNEL", "value": bridge_env["MM_CHANNEL_OPS"]},
            {"name": "MM_BOT_TOKEN", "valueFrom": {"secretKeyRef": {"name": "keep-mm-bridge", "key": "MM_BOT_TOKEN"}}}])
    (run / "configmap.json").write_text(json.dumps(configmap, indent=2))
    (run / "job.json").write_text(json.dumps(job, indent=2))
    (run / "source-manifest.json").write_text(json.dumps(manifest, indent=2))
    result = {"context": CONTEXT, "namespace": NAMESPACE, "job": name, "image": args.image,
              "image_id": image["Id"], "passed": False, "completed": False, "run": str(run)}
    (run / "result.json").write_text(json.dumps(result, indent=2))
    (run.parent / "CURRENT").write_text(str(run) + "\n")
    print(f"Apply isolated ConfigMap/Job to {CONTEXT}/{NAMESPACE}: {name}", flush=True)
    command(kubectl + ["apply", "--server-side", "--field-manager=keep-lab-check", "-f", str(run / "configmap.json")], "configmap-apply.log")
    command(kubectl + ["apply", "-f", str(run / "job.json")], "job-apply.log")
    deadline = time.monotonic() + (1330 if migration else 630 if correlation else 360 if integrations else 150)
    next_log = 0
    while time.monotonic() < deadline:
        status = json.loads(command(kubectl + ["-n", NAMESPACE, "get", "job", name, "-o", "json"]).stdout)
        (run / "job-result.json").write_text(json.dumps(status, indent=2))
        condition = next((item for item in status.get("status", {}).get("conditions", [])
                          if item["type"] in {"Complete", "Failed", "FailureTarget"} and item["status"] == "True"), None)
        if time.monotonic() >= next_log or condition:
            captured = command(kubectl + ["-n", NAMESPACE, "logs", "job/" + name, "-c", "verify", "--pod-running-timeout=5s"], check=False)
            if captured.returncode == 0:
                (run / "checks.log").write_text(captured.stdout + captured.stderr)
            next_log = time.monotonic() + 15
        if condition:
            result.update(completed=True, passed=condition["type"] == "Complete", condition=condition["type"])
            break
        time.sleep(2)
    log = command(kubectl + ["-n", NAMESPACE, "logs", "job/" + name, "-c", "verify", "--pod-running-timeout=5s"], check=False)
    if log.returncode == 0:
        (run / "checks.log").write_text(log.stdout + log.stderr)
    elif not (run / "checks.log").exists():
        (run / "checks.log").write_text(log.stdout + log.stderr)
    pods = command(kubectl + ["-n", NAMESPACE, "get", "pods", "-l", "job-name=" + name, "-o", "json"], "pods.json")
    result["pods"] = [{"name": pod["metadata"]["name"], "node": pod["spec"].get("nodeName"),
                       "phase": pod.get("status", {}).get("phase"),
                       "containers": pod.get("status", {}).get("containerStatuses", [])}
                      for pod in json.loads(pods.stdout)["items"]]
    (run / "result.json").write_text(json.dumps(result, indent=2))
    print((run / "checks.log").read_text()[-9000:], flush=True)
    print(json.dumps({key: result[key] for key in ("context", "namespace", "job", "completed", "passed", "run")}, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
