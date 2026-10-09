"""Restart only the local thin transport and verify PVC/post bindings survive."""
import base64
import importlib.util
import json
from pathlib import Path
import subprocess
import time
import urllib.request
from urllib.parse import urlsplit

spec = importlib.util.spec_from_file_location('prepare', Path(__file__).with_name('prepare-core-lab.py'))
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)
RUN, KUBE = prepare.RUN, prepare.KUBE


def main():
    config = json.loads(prepare.kube('config', 'view', '--minify', '-o', 'json'))
    assert urlsplit(config['clusters'][0]['cluster']['server']).hostname in {'0.0.0.0', 'localhost', '127.0.0.1'}
    secret = json.loads(prepare.kube('get', 'secret', 'keep-mm-bridge', '-o', 'json'))
    token = base64.b64decode(secret['data']['MM_BOT_TOKEN']).decode()
    channels = set(json.loads((RUN / 'channels.json').read_text()).values())

    def posts():
        result = {}
        for channel in channels:
            request = urllib.request.Request('http://localhost:8065/api/v4/channels/' + channel + '/posts?per_page=200',
                headers={'Authorization': 'Bearer ' + token})
            with urllib.request.urlopen(request, timeout=10) as response:
                page = json.load(response)
            result[channel] = sorted(page['posts'])
        return result

    def state():
        code = """
import json,sqlite3
db=sqlite3.connect('file:/state/transport.db?mode=ro',uri=True)
print(json.dumps({'bindings':db.execute('select id,incident_id,team_id,destination,external_id from binding order by id').fetchall(),
    'deliveries':db.execute('select state,count(*) from delivery group by state').fetchall(),
    'service_posts':db.execute('select id,external_id from silence_post order by id').fetchall()}))
"""
        return json.loads(prepare.kube('exec', '-i', 'deployment/keep-notification-bridge', '--', 'python', '-', payload=code))

    until = time.monotonic() + 150
    while True:
        count = prepare.kube('exec', 'deployment/keep-postgres', '--', 'psql', '-U', 'keep', '-d', 'keep', '-tAc',
            "SELECT count(*) FROM notificationdelivery WHERE state IN ('pending','leased')").strip()
        if count == '0':
            break
        assert time.monotonic() < until, 'Wait for local sender to drain before restarting the transport'
        time.sleep(2)
    before = {'state': state(), 'posts': posts()}
    assert before['state']['bindings'], 'No real bindings to verify'
    (RUN / 'bridge-state.before.json').write_text(json.dumps(before, indent=2) + '\n')
    print('kubectl --context k3d-local -n keep-lab rollout restart deployment/keep-notification-bridge', flush=True)
    prepare.kube('rollout', 'restart', 'deployment/keep-notification-bridge')
    result = subprocess.run(KUBE + ['rollout', 'status', 'deployment/keep-notification-bridge', '--timeout=60s'],
        capture_output=True, text=True)
    (RUN / 'bridge-restart.log').write_text(result.stdout + result.stderr)
    assert result.returncode == 0, 'Local transport failed to restart'
    # Let one reconciliation cycle run before checking the same external effects.
    time.sleep(12)
    after = {'state': state(), 'posts': posts()}
    (RUN / 'bridge-state.after.json').write_text(json.dumps(after, indent=2) + '\n')
    checks = {'bindings_preserved': before['state']['bindings'] == after['state']['bindings'],
        'service_post_ids_preserved': before['state']['service_posts'] == after['state']['service_posts'],
        'no_duplicate_posts': before['posts'] == after['posts'],
        'ordinary_bindings': len(before['state']['bindings']), 'service_posts': len(before['state']['service_posts'])}
    checks['passed'] = all(checks[k] for k in ('bindings_preserved', 'service_post_ids_preserved', 'no_duplicate_posts'))
    (RUN / 'restart-checks.json').write_text(json.dumps(checks, indent=2) + '\n')
    print(json.dumps(checks), flush=True)
    assert checks['passed']


if __name__ == '__main__':
    main()
