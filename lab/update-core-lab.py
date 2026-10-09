"""Update only the backed-up local lab; policy activation is a separate preview/apply."""
import importlib.util
import argparse
import json
from pathlib import Path
import secrets
import subprocess
import yaml

spec = importlib.util.spec_from_file_location('prepare', Path(__file__).with_name('prepare-core-lab.py'))
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)
ROOT, RUN, OUT, KUBE = prepare.ROOT, prepare.RUN, prepare.OUT, prepare.KUBE


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='keep-backend:lab-core-20261007-r2')
    args = parser.parse_args()
    assert (RUN / 'keep.before.dump').stat().st_size > 1_000_000, 'Database backup required'
    config = json.loads(prepare.kube('config', 'view', '--minify', '-o', 'json'))
    from urllib.parse import urlsplit
    assert urlsplit(config['clusters'][0]['cluster']['server']).hostname in {'0.0.0.0', '127.0.0.1', 'localhost'}
    current_secret = subprocess.run(KUBE + ['get', 'secret', 'keep-core-bridge', '-o', 'name'], capture_output=True)
    if current_secret.returncode:
        prepare.kube('apply', '-f', '-', payload=json.dumps({'apiVersion': 'v1', 'kind': 'Secret',
            'metadata': {'name': 'keep-core-bridge', 'namespace': 'keep-lab'}, 'type': 'Opaque',
            'stringData': {'transport': secrets.token_urlsafe(36), 'service': secrets.token_urlsafe(36)}}))
    paths = sorted(OUT.rglob('*.yaml'))
    items = [{'key': 'file-' + str(i), 'path': str(path.relative_to(OUT))} for i, path in enumerate(paths)]
    cm = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'keep-incident-core', 'namespace': 'keep-lab'},
        'data': {item['key']: path.read_text() for item, path in zip(items, paths)}}
    prepare.kube('apply', '-f', '-', payload=json.dumps(cm))
    env = [{'name': name, 'valueFrom': {'secretKeyRef': {'name': 'keep-core-bridge', 'key': key}}}
        for name, key in [('BRIDGE_TRANSPORT_TOKEN', 'transport'), ('KEEP_BRIDGE_SERVICE_TOKEN', 'service')]]
    env += [{'name': 'KEEP_IMPERSONATION_ENABLED', 'value': 'false'},
        {'name': 'TMPDIR', 'value': '/state/tmp'}, {'name': 'PROMETHEUS_MULTIPROC_DIR', 'value': '/state/prometheus'}]
    patch = {'spec': {'template': {'spec': {'containers': [{'name': 'keep', 'image': args.image,
        'imagePullPolicy': 'Never', 'env': env,
        'volumeMounts': [{'name': 'incident-core', 'mountPath': '/configuration/incident-core', 'readOnly': True}]}],
        'volumes': [{'name': 'incident-core', 'configMap': {'name': 'keep-incident-core', 'items': items}}]}}}}
    patch_file = RUN / 'backend-core.patch.json'
    patch_file.write_text(json.dumps(patch, indent=2))
    print('kubectl --context k3d-local -n keep-lab patch deployment keep-backend --type strategic --patch-file ' + str(patch_file), flush=True)
    prepare.kube('patch', 'deployment', 'keep-backend', '--type', 'strategic', '--patch-file', str(patch_file))
    result = subprocess.run(KUBE + ['rollout', 'status', 'deployment/keep-backend', '--timeout=60s'], capture_output=True, text=True)
    (RUN / 'backend-rollout.log').write_text(result.stdout + result.stderr)
    assert result.returncode == 0, 'Backend rollout failed; inspect persistent log'
    print('Backend updated; existing IncidentPolicies activation retained', flush=True)


if __name__ == '__main__':
    main()
