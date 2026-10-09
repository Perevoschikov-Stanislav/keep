"""Render the Enterprise deployment draft locally, with source secrets redacted."""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime, timezone

import yaml


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "config/incident-deploy.example"


def run(args, directory, environment):
    result = subprocess.run(args, cwd=directory, env=environment, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr[:2000] or result.stdout[:2000])
    return result.stdout


def documents(text):
    return [item for item in yaml.safe_load_all(text) if item]


def resource(items, kind, name):
    matches = [item for item in items if item["kind"] == kind and item["metadata"]["name"] == name]
    assert len(matches) == 1, (kind, name, len(matches))
    return matches[0]


def redact_manifests(items):
    items = copy.deepcopy(items)

    def redact_env(value):
        if isinstance(value, dict):
            name = value.get("name", "")
            if isinstance(name, str) and name.endswith(("_SECRET", "_TOKEN", "_PASSWORD", "_PASSWD")) and "value" in value:
                value["value"] = "REDACTED"
            for child in value.values():
                redact_env(child)
        elif isinstance(value, list):
            for child in value:
                redact_env(child)

    redact_env(items)
    for item in items:
        if item["kind"] == "Secret":
            for field in ("data", "stringData"):
                if field in item:
                    item[field] = {key: "REDACTED" for key in item[field]}
    return yaml.safe_dump_all(items, sort_keys=False, allow_unicode=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dependencies", required=True, type=Path, help="Directory containing the three exact Helm archives")
    parser.add_argument("--source-dir", required=True, type=Path, help="Read-only parent chart checkout")
    parser.add_argument("--example-dir", type=Path, default=EXAMPLE, help="Public template or private prepared draft")
    args = parser.parse_args()
    example = args.example_dir.resolve()
    provenance = json.loads((example / "source.json").read_text())
    source = args.source_dir.resolve()
    source_hashes = {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in
        ("Chart.yaml", "values.yaml", "templates/keep-mm.yaml", "templates/gitops.yaml", "templates/ingress.yaml", "templates/keep-mm-bot.yaml")}

    directory = ROOT / ".lab-work/core-deploy-example" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory.mkdir(parents=True)
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    for variable, name in {"TMPDIR": "tmp", "HELM_CACHE_HOME": "helm-cache", "HELM_CONFIG_HOME": "helm-config", "HELM_DATA_HOME": "helm-data"}.items():
        path = directory / name
        path.mkdir()
        environment[variable] = str(path)

    fixture = directory / "keep"
    (fixture / "templates").mkdir(parents=True)
    (fixture / "Chart.yaml").write_bytes((source / "Chart.yaml").read_bytes())
    raw_values = (source / "values.yaml").read_text()
    safe_values, count = re.subn(r'(?m)^(\s+- name: PUSHER_APP_SECRET\n\s+value: ).*$', r'\1"EXAMPLE_ONLY_NOT_A_SECRET"', raw_values)
    assert count == 1, "Source secret layout changed"
    (fixture / "values.yaml").write_text(safe_values)
    for name in ("keep-mm.yaml", "gitops.yaml", "ingress.yaml", "keep-db.yaml", "secrets.yaml", "keep-mm-bot.yaml"):
        text = (source / "templates" / name).read_text()
        if name == "keep-mm-bot.yaml":
            text, count = re.subn(r'(?m)^(\s+MM_BOT_TOKEN: ).*$', r'\1"EXAMPLE_ONLY_NOT_A_TOKEN"', text)
            assert count == 1, "Source bot secret layout changed"
        (fixture / "templates" / name).write_text(text)
    run(["patch", "--dry-run", "--batch", "-p1", "-i", str(example / "keep-wrapper.patch")], fixture, environment)
    run(["patch", "--batch", "-p1", "-i", str(example / "keep-wrapper.patch")], fixture, environment)
    shutil.copytree(example / "chart-files", fixture, dirs_exist_ok=True)
    charts = fixture / "charts"
    charts.mkdir()
    for dependency in provenance["dependencies"]:
        archive = args.dependencies.resolve() / (dependency["name"] + "-" + dependency["version"] + ".tgz")
        with tarfile.open(archive) as package:
            metadata = yaml.safe_load(package.extractfile(dependency["name"] + "/Chart.yaml").read())
        assert (metadata["name"], metadata["version"]) == (dependency["name"], dependency["version"])
        shutil.copyfile(archive, charts / archive.name)

    overlay_values = yaml.safe_load((example / "values.overlay.yaml").read_text())
    overlay = str(example / "values.overlay.yaml")
    run(["helm", "lint", str(fixture), "-f", overlay], ROOT, environment)
    rendered = documents(run(["helm", "template", "keep", str(fixture), "--namespace", "monitoring", "-f", overlay], ROOT, environment))
    (directory / "keep.rendered.redacted.yaml").write_text(redact_manifests(rendered))
    assert not any(item["kind"] == "Job" and item["metadata"]["name"] == "keep-gitops-apply" for item in rendered)
    assert not any(item["kind"] == "CronJob" and item["metadata"]["name"] == "keep-ghosts" for item in rendered)
    assert resource(rendered, "Deployment", "keep-mm-bridge")["spec"]["replicas"] == 0
    backend = resource(rendered, "Deployment", "keep-backend")["spec"]["template"]["spec"]
    env = {entry["name"]: entry for entry in backend["containers"][0]["env"]}
    assert env["KEEP_OAUTH2_PROXY_ADMIN_ROLES"]["value"] == "/keep-admin"
    assert env["KEEP_IMPERSONATION_ENABLED"]["value"] == "false"
    assert "keep-mm:admin:" not in env["KEEP_DEFAULT_API_KEYS"]["value"]
    for key, field in (("BRIDGE_TRANSPORT_TOKEN", "transport"), ("KEEP_BRIDGE_SERVICE_TOKEN", "service")):
        assert env[key]["valueFrom"]["secretKeyRef"] == {"name": "keep-core-bridge", "key": field}
    original_env = {entry["name"]: entry for entry in yaml.safe_load(safe_values)["keep"]["backend"]["env"]}
    for key, entry in original_env.items():
        if key.startswith(("DATABASE_", "REDIS_")):
            expected = {"name": key, "valueFrom": {"secretKeyRef": {"name": entry["secretKeyRef"]["secretName"], "key": entry["secretKeyRef"]["secretKey"]}}} if "secretKeyRef" in entry else entry
            assert env[key] == expected, key
    backend_config = resource(rendered, "ConfigMap", "keep-backend-env")["data"]
    assert backend_config["PROVISION_RESOURCES"] == "false"
    assert backend_config["KEEP_INCIDENT_POLICIES_CONFIG_FILE"] == "/configuration/incident-core/bundle.yaml"
    assert backend_config["TMPDIR"] == "/state"
    for key in ("KEEP_OSS_ONLY", "POSTHOG_DISABLED", "SENTRY_DISABLED"):
        assert env[key]["value"] == "true", key
    mounts = backend["containers"][0]["volumeMounts"]
    assert {"name": "incident-core", "mountPath": "/configuration/incident-core", "readOnly": True} in mounts
    core_data = resource(rendered, "ConfigMap", "keep-incident-core")["data"]
    policy_volume = next(volume for volume in backend["volumes"] if volume["name"] == "incident-core")
    policy_files = {str(path.relative_to(example / "chart-files/incident-core"))
                    for path in (example / "chart-files/incident-core").rglob("*.yaml")}
    assert {item["path"] for item in policy_volume["configMap"]["items"]} == policy_files
    assert len(policy_volume["configMap"]["items"]) == len(core_data)
    mounted = directory / "mounted-policy"
    for item in policy_volume["configMap"]["items"]:
        assert core_data[item["key"]] == (example / "chart-files/incident-core" / item["path"]).read_text(), "ConfigMap changes file bytes: " + item["path"]
        target = mounted / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(core_data[item["key"]])
    oauth = resource(rendered, "Deployment", "keep-oauth2-proxy")
    assert "--oidc-groups-claim=keep_groups" in oauth["spec"]["template"]["spec"]["containers"][0]["args"]
    for side in ("backend", "frontend"):
        pod = resource(rendered, "Deployment", "keep-" + side)["spec"]["template"]["spec"]
        assert pod["imagePullSecrets"] == overlay_values["keep"][side]["imagePullSecrets"]
        expected_image = overlay_values["keep"][side]["image"]
        assert pod["containers"][0]["image"] == expected_image["repository"] + ":" + expected_image["tag"]

    bridge_chart = directory / "transport-chart"
    shutil.copytree(ROOT / "transports/mattermost/chart", bridge_chart)
    run(["patch", "--dry-run", "--batch", "-p1", "-i", str(example / "transport-chart.patch")], bridge_chart, environment)
    run(["patch", "--batch", "-p1", "-i", str(example / "transport-chart.patch")], bridge_chart, environment)
    bridge_configuration = yaml.safe_load((example / "bridge-values.yaml").read_text())
    bridge_values = str(example / "bridge-values.yaml")
    run(["helm", "lint", str(bridge_chart), "-f", bridge_values], ROOT, environment)
    bridge_rendered = documents(run(["helm", "template", "keep-notification-bridge", str(bridge_chart), "--namespace", "monitoring", "-f", bridge_values], ROOT, environment))
    (directory / "bridge.rendered.yaml").write_text(redact_manifests(bridge_rendered))
    bridge_pod = resource(bridge_rendered, "Deployment", "keep-notification-bridge")["spec"]["template"]["spec"]
    assert bridge_pod["imagePullSecrets"] == bridge_configuration["imagePullSecrets"]
    expected_image = bridge_configuration["image"]
    assert bridge_pod["containers"][0]["image"] == expected_image["repository"] + ":" + expected_image["tag"]
    assert resource(bridge_rendered, "PersistentVolumeClaim", "keep-notification-bridge-state")["spec"]["storageClassName"] == bridge_configuration["persistence"]["storageClassName"]
    bridge_env = {entry["name"]: entry for entry in bridge_pod["containers"][0]["env"]}
    assert bridge_env["MM_BOT_TOKEN"]["valueFrom"]["secretKeyRef"] == {"name": "keep-mm-bot", "key": "MM_BOT_TOKEN"}
    for key in ("BRIDGE_TRANSPORT_TOKEN", "KEEP_BRIDGE_SERVICE_TOKEN"):
        assert bridge_env[key]["valueFrom"] == env[key]["valueFrom"]

    validation = json.loads(run([sys.executable, "-B", "lab/incident_contract.py", "validate", str(example / "chart-files/incident-core/bundle.yaml"), "--tenant", "keep"], ROOT, environment))
    mounted_validation = json.loads(run([sys.executable, "-B", "lab/incident_contract.py", "validate", str(mounted / "bundle.yaml"), "--tenant", "keep"], ROOT, environment))
    assert mounted_validation == validation, "Mounted policy differs from source"
    teams = yaml.safe_load((example / "chart-files/incident-core/teams.yaml").read_text())
    for role in ("responder", "viewer"):
        assert backend_config["KEEP_OAUTH2_PROXY_" + role.upper() + "_ROLES"] == ",".join(teams["roles"][role])
    role_groups = {path for paths in teams["roles"].values() for path in paths}
    groups = json.loads((example / "keycloak/groups.fragment.json").read_text())["groups"]
    assert role_groups == {"/" + item["name"] for item in groups if "subGroups" not in item}
    team_groups = {path for team in teams["teams"] for path in team["groups"]}
    assert team_groups == {"/" + item["name"] + "/" + child["name"] for item in groups for child in item.get("subGroups", [])}
    mapper = json.loads((example / "keycloak/keep-client-mapper.fragment.json").read_text())
    assert mapper["config"]["claim.name"] == "keep_groups" and mapper["config"]["full.path"] == "true"
    for name, expected in source_hashes.items():
        assert hashlib.sha256((source / name).read_bytes()).hexdigest() == expected, "Source changed during check: " + name
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "result": "PASS", "dependencies": provenance["dependencies"], "policy": validation, "checks": ["both patches apply", "both charts lint and render", "roles, proxy claim and Keycloak groups agree", "legacy sender and PostSync job disabled", "credentials and private image pull refs", "database and Redis env preserved", "all policy files mounted with exact bytes and valid hashes", "source file hashes unchanged"], "output": str(directory)}
    (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    (directory.parent / "CURRENT").write_text(str(directory) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
