#!/usr/bin/env python3
"""Generate an isolated silence verification k3d manifest; never apply it implicitly."""

import argparse
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = "keep-silences-22"


def resource(kind, name, **fields):
    return {"apiVersion": "apps/v1" if kind == "Deployment" else "v1", "kind": kind,
            "metadata": {"name": name, "namespace": NAMESPACE}, **fields}


def deployment(name, image, port, env=None, command=None, uid=1000, volumes=None, mounts=None):
    container = {"name": name, "image": image, "imagePullPolicy": "Never",
                 "ports": [{"containerPort": port}],
                 "env": [{"name": key, "value": str(value)} for key, value in (env or {}).items()],
                 "securityContext": {"runAsUser": uid, "allowPrivilegeEscalation": False,
                                     "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
                 "volumeMounts": [{"name": "work", "mountPath": "/work"}, *(mounts or [])],
                 "readinessProbe": {"tcpSocket": {"port": port}, "periodSeconds": 2},
                 "resources": {"requests": {"cpu": "50m", "memory": "128Mi"}}}
    if command:
        container["command"] = command
    return resource("Deployment", name, spec={"replicas": 1,
        "strategy": {"type": "Recreate"}, "selector": {"matchLabels": {"app": name}},
        "template": {"metadata": {"labels": {"app": name}}, "spec": {
            "automountServiceAccountToken": False, "securityContext": {"fsGroup": uid},
            "containers": [container], "volumes": [{"name": "work", "emptyDir": {}}, *(volumes or [])]}}})


def service(name, port):
    return resource("Service", name, spec={"selector": {"app": name},
        "ports": [{"port": port, "targetPort": port}]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--backend-image", required=True)
    parser.add_argument("--frontend-image", required=True)
    parser.add_argument("--cutover", action="store_true", help="Disable legacy event dropping after reviewed migration")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    if not output.is_relative_to(ROOT / ".lab-work"):
        parser.error("Artifacts must be inside the fork's .lab-work")
    output.mkdir(parents=True, exist_ok=True)

    policy = {"version": 1, "visibility": "team", "roles": {
        role: ["/roles/" + role] for role in ("admin", "responder", "viewer")},
        "teams": [{"id": team, "groups": ["/teams/" + team], "zones": ["zone-" + team],
                   "visible_to": [team]} for team in ("cedar", "quartz")]}
    # Cedar data can be read by Quartz; Cedar cannot read Quartz data.
    policy["teams"][0]["visible_to"].append("quartz")
    policy_text = json.dumps(policy, indent=2) + "\n"
    bundle = {"api_version": "keep.incidents/v1", "kind": "IncidentPolicies",
        "id": "silences-ui-lab", "tenant_id": "keep", "revision": "task-22",
        "keep_url": "http://localhost:8012", "access": {"path": "teams.yaml",
        "sha256": hashlib.sha256(policy_text.encode()).hexdigest()},
        "transports": [{"id": "local-events", "kind": "http_json", "adapter_ref": "http-json-v1",
            "endpoint": "http://receiver:8090", "auth_ref": None,
            "capabilities": {"update": False, "actions": False, "receipts": False},
            "delivery": {"timeout_seconds": 2, "retry": {"max_attempts": 5,
                "initial_backoff_seconds": 1, "max_backoff_seconds": 5, "multiplier": 2},
                "rate_limit": {"per_second": 10, "burst": 10}, "debounce_seconds": 0}}],
        "destinations": [{"id": team + "-events", "team_id": team,
            "transport_ref": "local-events", "options": {"path": "/events/" + team}}
            for team in ("cedar", "quartz")],
        "subscribers": [{"id": "lab-state", "team_ids": ["cedar", "quartz"],
            "event_types": ["silence." + state for state in
                ("created", "updated", "activated", "cancelled", "expired")],
            "destination_refs": [team + "-events" for team in ("cedar", "quartz")]}],
        "dispatch": {"scan_interval_seconds": 1, "batch_size": 50, "lease_seconds": 10}}
    nginx = '''events {}
http {
  access_log off;
  client_body_temp_path /work/body;
  proxy_temp_path /work/proxy;
  fastcgi_temp_path /work/fastcgi;
  uwsgi_temp_path /work/uwsgi;
  scgi_temp_path /work/scgi;
  map $cookie_silence_lab_actor $lab_groups {
    default "";
    admin /roles/admin;
    cedar /roles/responder,/teams/cedar;
    quartz /roles/responder,/teams/quartz;
    viewer /roles/viewer,/teams/cedar;
  }
  map $cookie_silence_lab_actor $lab_email {
    default "";
    admin admin@example.test;
    cedar cedar@example.test;
    quartz quartz@example.test;
    viewer viewer@example.test;
  }
  server {
    listen 8000;
    location /v2/ {
      if ($lab_email = "") { return 401; }
      proxy_set_header X-Auth-Request-Email $lab_email;
      proxy_set_header X-Auth-Request-Groups $lab_groups;
      proxy_set_header Authorization "";
      rewrite ^/v2/(.*)$ /$1 break;
      proxy_pass http://backend:8080;
    }
    location / {
      if ($lab_email = "") { return 401; }
      proxy_set_header X-Auth-Request-Email $lab_email;
      proxy_set_header X-Auth-Request-Groups $lab_groups;
      proxy_set_header Authorization "";
      proxy_set_header Host $http_host;
      proxy_set_header X-Forwarded-Proto $scheme;
      proxy_pass http://frontend:3000;
    }
  }
}
'''
    config_data = {"teams.yaml": policy_text, "integrations.yaml": json.dumps(bundle),
                   "nginx.conf": nginx, "receiver.py": (ROOT / "lab/silences-ui-receiver.py").read_text()}
    work_env = {"TMPDIR": "/work", "TEMP": "/work", "TMP": "/work",
                "PYTHONDONTWRITEBYTECODE": "1"}
    backend_env = {**work_env, "AUTH_TYPE": "OAUTH2PROXY", "KEEP_OSS_ONLY": "true",
        "EE_ENABLED": "false", "POSTHOG_DISABLED": "true", "SENTRY_DISABLED": "true",
        "KEEP_OTEL_ENABLED": "false", "KEEP_TEAMS_CONFIG_FILE": "/config/teams.yaml",
        "KEEP_SILENCES_INTEGRATIONS_CONFIG_FILE": "/config/integrations.yaml",
        "KEEP_OAUTH2_PROXY_USER_HEADER": "X-Auth-Request-Email",
        "KEEP_OAUTH2_PROXY_ROLE_HEADER": "X-Auth-Request-Groups",
        "DATABASE_CONNECTION_STRING": "postgresql://keep@postgres:5432/keep",
        "PROMETHEUS_MULTIPROC_DIR": "/work/prometheus", "PROVISION_RESOURCES": "false",
        "SECRET_MANAGER_DIRECTORY": "/work/secrets", "SCHEDULER": "true", "WATCHER": "false",
        "MAINTENANCE_WINDOWS": "false", "KEEP_MAINTENANCE_WINDOWS_ENABLED": "true",
        "KEEP_MAINTENANCE_DESTRUCTIVE_DROP": "false" if args.cutover else "true",
        "PUSHER_DISABLED": "true", "KEEP_STORAGE_DIRECTORY": "/work/storage"}
    cm_volume = {"name": "config", "configMap": {"name": "silences-lab"}}
    cm_mount = {"name": "config", "mountPath": "/config", "readOnly": True}
    items = [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE}},
        resource("ConfigMap", "silences-lab", data=config_data),
        resource("PersistentVolumeClaim", "postgres-data", spec={"accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": "1Gi"}}}),
        deployment("postgres", "postgres:15-alpine", 5432, uid=70,
            env={**work_env, "POSTGRES_USER": "keep", "POSTGRES_DB": "keep",
                "POSTGRES_HOST_AUTH_METHOD": "trust", "PGDATA": "/pgdata/data", "PGHOST": "/work"},
            command=["docker-entrypoint.sh", "postgres", "-c", "unix_socket_directories=/work"],
            volumes=[{"name": "pgdata", "persistentVolumeClaim": {"claimName": "postgres-data"}}],
            mounts=[{"name": "pgdata", "mountPath": "/pgdata"},
                    {"name": "work", "mountPath": "/var/run/postgresql"}]),
        service("postgres", 5432),
        deployment("receiver", args.backend_image, 8090, env=work_env,
            command=["python", "/config/receiver.py"], volumes=[cm_volume], mounts=[cm_mount]),
        service("receiver", 8090),
        deployment("backend", args.backend_image, 8080, env=backend_env,
            command=["gunicorn", "keep.api.api:get_app", "--bind", "0.0.0.0:8080", "--workers", "2",
                "-k", "uvicorn.workers.UvicornWorker", "-c",
                "/venv/lib/python3.13/site-packages/keep/api/config.py", "--preload"],
            volumes=[cm_volume], mounts=[cm_mount]), service("backend", 8080),
        deployment("frontend", args.frontend_image, 3000, uid=1001,
            env={**work_env, "AUTH_TYPE": "OAUTH2PROXY", "API_URL": "http://backend:8080",
                "API_URL_CLIENT": "/v2", "AUTH_TRUST_HOST": "true", "NEXTAUTH_URL": "http://localhost:8012",
                "KEEP_OAUTH2_PROXY_EMAIL_HEADER": "X-Auth-Request-Email",
                "KEEP_OAUTH2_PROXY_ROLE_HEADER": "X-Auth-Request-Groups",
                "NEXTAUTH_SECRET": "local-lab-fixture-not-a-real-secret",
                "KEEP_OSS_ONLY": "true", "POSTHOG_DISABLED": "true", "SENTRY_DISABLED": "true",
                "NEXT_TELEMETRY_DISABLED": "1", "PUSHER_DISABLED": "true"}), service("frontend", 3000),
        deployment("gateway", "nginx:1.27-alpine", 8000,
            command=["nginx", "-c", "/config/nginx.conf", "-g", "daemon off; pid /work/nginx.pid;"],
            volumes=[cm_volume], mounts=[cm_mount]), service("gateway", 8000)]
    (output / "manifest.json").write_text(json.dumps({"apiVersion": "v1", "kind": "List", "items": items}, indent=2))
    (output / "commands.txt").write_text(
        f"kubectl --context k3d-local --cache-dir={ROOT / '.lab-work/kube-cache'} apply -f {output / 'manifest.json'}\n"
        f"kubectl --context k3d-local -n {NAMESPACE} port-forward --address 127.0.0.1 svc/gateway 8012:8000\n"
        "The gateway cookie adapter is a test fixture, never a production SSO configuration.\n"
        "The default legacy-drop flag is true; --cutover selects false after reviewed migration.\n")
    print(output / "manifest.json")


if __name__ == "__main__":
    main()
