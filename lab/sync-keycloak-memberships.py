"""Restore additive lab memberships and the realm import from the IaC YAML."""

import argparse
import copy
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import quote

import requests
import yaml


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memberships", type=Path, default=ROOT / "lab/keycloak-memberships.yaml")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    desired = yaml.safe_load(args.memberships.read_text())
    realm = desired["realm"]
    if not isinstance(realm, str) or not re.fullmatch(r"[\w-]+", realm):
        raise ValueError("Invalid realm name")
    memberships = desired["users"]
    for username, paths in memberships.items():
        if not isinstance(username, str) or not isinstance(paths, list):
            raise ValueError("Invalid user memberships")
        for path in paths:
            if not isinstance(path, str) or not re.fullmatch(r"/[\w.-]+(?:/[\w.-]+)*", path):
                raise ValueError("Memberships must contain full group paths")
            if any(part in {".", ".."} for part in path.split("/")[1:]):
                raise ValueError("Invalid group path")

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run = ROOT / ".lab-work/keycloak-memberships" / stamp
    run.mkdir(parents=True, mode=0o700)
    (run / "tmp").mkdir()
    os.environ.update(TMPDIR=str(run / "tmp"), TMP=str(run / "tmp"), TEMP=str(run / "tmp"))
    kube = ["kubectl", "--context", "k3d-local", "--cache-dir=" + str(run / "kube-cache"),
            "--request-timeout=20s", "-n", "keep-lab"]

    def get(kind, name):
        return json.loads(subprocess.check_output(kube + ["get", kind, name, "-o", "json"]))

    configmap = get("configmap", "keycloak-realm")
    key, document = next((k, json.loads(v)) for k, v in configmap["data"].items()
                         if json.loads(v).get("realm") == realm)
    candidate = copy.deepcopy(document)
    users = {user["username"]: user for user in candidate["users"]}
    for username, paths in memberships.items():
        if username not in users:
            raise ValueError("A desired lab user is absent from the realm import")
        user = users[username]
        user["groups"] = sorted(set("/" + p.lstrip("/") for p in user.get("groups", [])) | set(paths))
        for path in paths:
            groups = candidate.setdefault("groups", [])
            for part in path.split("/")[1:]:
                group = next((g for g in groups if g["name"] == part), None)
                if group is None:
                    group = {"name": part, "subGroups": []}
                    groups.append(group)
                groups = group.setdefault("subGroups", [])

    deployment = get("deployment", "keycloak")
    env = {item["name"]: item.get("value") for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
    client = requests.Session()
    client.trust_env = False
    token = client.post("http://127.0.0.1:8180/realms/master/protocol/openid-connect/token",
                        data={"grant_type": "password", "client_id": "admin-cli",
                              "username": env["KC_BOOTSTRAP_ADMIN_USERNAME"],
                              "password": env["KC_BOOTSTRAP_ADMIN_PASSWORD"]}, timeout=20)
    if token.status_code != 200:
        raise RuntimeError("Lab Keycloak administration login failed")
    client.headers["Authorization"] = "Bearer " + token.json()["access_token"]
    base = "http://127.0.0.1:8180/admin/realms/" + quote(realm, safe="")

    def request(method, endpoint, **kwargs):
        response = client.request(method, base + endpoint, timeout=20, allow_redirects=False, **kwargs)
        if response.status_code not in {200, 201, 204}:
            raise RuntimeError(f"Keycloak {method} failed with HTTP {response.status_code}")
        return response

    report = []
    user_ids = {}
    for username, paths in memberships.items():
        found = request("GET", "/users", params={"username": username, "exact": "true"}).json()
        if len(found) != 1 or found[0]["username"] != username:
            raise ValueError("A desired lab user is absent or ambiguous in live Keycloak")
        user_ids[username] = found[0]["id"]
        current = request("GET", "/users/" + user_ids[username] + "/groups", params={"max": 1000}).json()
        report.append({"username": username, "current": sorted(g["path"] for g in current),
                       "add": sorted(set(paths) - {g["path"] for g in current})})
    (run / "memberships-before.json").write_text(json.dumps(report, indent=2) + "\n")

    if args.apply:
        backup = run / "realm-configmap-before.json"
        backup.write_text(json.dumps(configmap, indent=2) + "\n")
        backup.chmod(0o600)
        for item in report:
            for path in item["add"]:
                parent = None
                for part in path.split("/")[1:]:
                    endpoint = "/groups" if parent is None else "/groups/" + parent + "/children"
                    found = request("GET", endpoint, params={"search": part, "exact": "true", "max": 1000}).json()
                    group = next((g for g in found if g["name"] == part), None)
                    if group is None:
                        request("POST", endpoint, json={"name": part})
                        found = request("GET", endpoint, params={"search": part, "exact": "true", "max": 1000}).json()
                        group = next(g for g in found if g["name"] == part)
                    parent = group["id"]
                request("PUT", "/users/" + user_ids[item["username"]] + "/groups/" + parent)
        if candidate != document:
            patch = run / "realm-configmap-patch.json"
            patch.write_text(json.dumps({"data": {key: json.dumps(candidate)}}) + "\n")
            patch.chmod(0o600)
            subprocess.run(kube + ["patch", "configmap", "keycloak-realm", "--type=merge", "--patch-file", str(patch)], check=True)
        for username, paths in memberships.items():
            current = request("GET", "/users/" + user_ids[username] + "/groups", params={"max": 1000}).json()
            if not set(paths) <= {g["path"] for g in current}:
                raise RuntimeError("Desired memberships were not applied")
    summary = {"applied": args.apply, "memberships": report, "artifacts": str(run)}
    (run / "result.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
