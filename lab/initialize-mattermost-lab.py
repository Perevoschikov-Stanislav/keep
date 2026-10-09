"""Recreate local lab accounts/channels after an empty Preview DB; keep secrets in Kubernetes."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import urllib.error
import urllib.request
from urllib.parse import urlsplit

import yaml

spec = importlib.util.spec_from_file_location('prepare', Path(__file__).with_name('prepare-core-lab.py'))
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)
ROOT, RUN, KUBE = prepare.ROOT, prepare.RUN, prepare.KUBE


def main():
    config = json.loads(prepare.kube('config', 'view', '--minify', '-o', 'json'))
    assert urlsplit(config['clusters'][0]['cluster']['server']).hostname in {'0.0.0.0', '127.0.0.1', 'localhost'}
    origin = 'http://localhost:8065/api/v4'
    def mmctl(*args):
        result = subprocess.run(KUBE + ['exec', 'deployment/mattermost', '--', 'env',
            'TMPDIR=/mm/mattermost/data', 'XDG_CONFIG_HOME=/mm/mattermost/data/mmctl',
            'mmctl', '--local', '--json', *args], capture_output=True, text=True)
        assert result.returncode == 0, 'Local mmctl failed; credentials are never logged'
        return json.loads(result.stdout)
    def api(method, path, token, body=None):
        req = urllib.request.Request(origin + path, method=method,
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
            data=json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.load(response)
    result = subprocess.run(KUBE + ['get', 'secret', 'keep-mm-lab-admin', '-o', 'json'], capture_output=True, text=True)
    if result.returncode == 0:
        data = json.loads(result.stdout)['data']
        username, password = (base64.b64decode(data[key]).decode() for key in ('username', 'password'))
    else:
        username, password = 'lab-admin', secrets.token_urlsafe(32)
        prepare.kube('apply', '-f', '-', payload=json.dumps({'apiVersion': 'v1', 'kind': 'Secret',
            'metadata': {'name': 'keep-mm-lab-admin', 'namespace': 'keep-lab'},
            'stringData': {'username': username, 'password': password}}))
    if not any(user['username'] == username for user in mmctl('user', 'list')):
        mmctl('user', 'create', '--username', username, '--email', username + '@keep-lab.invalid',
            '--password', password, '--system-admin', '--email-verified', '--disable-welcome-email')
    secret = json.loads(prepare.kube('get', 'secret', 'keep-mm-bridge', '-o', 'json'))
    token = base64.b64decode(secret['data']['MM_BOT_TOKEN']).decode()
    try:
        api('GET', '/users/me', token)
    except urllib.error.HTTPError as error:
        assert error.code == 401, 'Unexpected MM credential failure'
        token = mmctl('token', 'generate', username, 'keep-lab-transport')[0]['token']
        prepare.kube('patch', 'secret', 'keep-mm-bridge', '--type', 'merge', '-p',
            json.dumps({'data': {'MM_BOT_TOKEN': base64.b64encode(token.encode()).decode()}}))
    teams = api('GET', '/users/me/teams', token)
    team = next((item for item in teams if item['name'] == 'keep-lab'), None)
    if not team:
        team = api('POST', '/teams', token, {'name': 'keep-lab', 'display_name': 'Keep lab', 'type': 'O'})
    def channel(name, display):
        try:
            return api('GET', '/teams/' + team['id'] + '/channels/name/' + name, token)
        except urllib.error.HTTPError as error:
            assert error.code == 404, 'Unexpected local channel lookup failure'
            return api('POST', '/channels', token, {'team_id': team['id'], 'name': name, 'display_name': display, 'type': 'P'})
    source = json.loads((ROOT / 'config/incident-core.target/source-routes.json').read_text())['routes']
    channels = {name: channel('core-' + name, 'Core lab · ' + name)['id'] for name in sorted({item['name'] for item in source})}
    directory = ROOT / 'config/incident-core.lab'
    bundle = yaml.safe_load((directory / 'bundle.yaml').read_text())
    bridge = yaml.safe_load((directory / 'bridge-values.yaml').read_text())
    for ref, destination in bridge['configuration']['destinations'].items():
        name = ref[len(destination['team_id']) + 1:]
        destination['channel_id'] = channels[name]
    for destination in bundle['destinations']:
        if 'channel_id' in destination['options']:
            destination['options']['channel_id'] = bridge['configuration']['destinations'][destination['id']]['channel_id']
    for name, data in [('bundle.yaml', bundle), ('bridge-values.yaml', bridge)]:
        (directory / name).write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
    legacy = {name: channel(name, name)['id'] for name in ('keep-ops', 'keep-it')}
    prepare.kube('patch', 'deployment', 'keep-mm-bridge', '--type', 'strategic', '-p', json.dumps({'spec': {'template': {'spec': {
        'containers': [{'name': 'bridge', 'env': [{'name': 'MM_CHANNEL_OPS', 'value': legacy['keep-ops']},
            {'name': 'MM_CHANNEL_IT', 'value': legacy['keep-it']}]}]}}}}))
    (RUN / 'channels.json').write_text(json.dumps(channels, indent=2) + '\n')
    print(json.dumps({'local_channels': len(channels), 'admin_secret': 'keep-mm-lab-admin', 'transport_secret': 'keep-mm-bridge'}))


if __name__ == '__main__':
    main()
