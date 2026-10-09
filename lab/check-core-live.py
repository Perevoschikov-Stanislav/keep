"""Real HTTP + real Mattermost checks on the explicitly local, upgraded lab."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

spec = importlib.util.spec_from_file_location('prepare', Path(__file__).with_name('prepare-core-lab.py'))
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)
ROOT, RUN = prepare.ROOT, prepare.RUN
PREFIX = 'core-live-' + RUN.name.lower()


def main():
    secret = json.loads(prepare.kube('get', 'secret', 'keep-mm-bridge', '-o', 'json'))['data']
    backend = json.loads(prepare.kube('get', 'deployment', 'keep-backend', '-o', 'json'))['spec']['template']['spec']['containers'][0]
    default_keys = next(item['value'] for item in backend['env'] if item['name'] == 'KEEP_DEFAULT_API_KEYS')
    key = default_keys.split(',')[0].split(':', 2)[2]
    token = base64.b64decode(secret['MM_BOT_TOKEN']).decode()
    checks = []
    def request(base, path, credential, method='GET', data=None):
        headers = {'Content-Type': 'application/json'}
        headers['X-API-KEY' if base.endswith(':8088') else 'Authorization'] = credential if base.endswith(':8088') else 'Bearer ' + credential
        req = urllib.request.Request(base + path, data=json.dumps(data).encode() if data is not None else None, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    def keep(path, method='GET', data=None):
        return request('http://localhost:8088', path, key, method, data)
    def mm(path):
        return request('http://localhost:8065', '/api/v4' + path, token)
    def check(label, condition):
        checks.append({'check': label, 'passed': bool(condition)})
        (RUN / 'live-checks.json').write_text(json.dumps({'checks': checks}, indent=2))
        assert condition, label
        print('PASS ' + label, flush=True)
    def eventually(label, fn, timeout=100):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            value = fn()
            if value:
                check(label, True)
                return value
            time.sleep(1)
        check(label, False)
    cases = [
        ('lis-case', 'LisCaseError', {'upstream': 'lis-lab', 'error_code': 'E-lab'}, 'integration-customer-a-incidents'),
        ('lis', 'LisUpstreamUnavailable', {'upstream': 'lis-lab'}, 'integration-customer-b-incidents'),
        ('workload', 'KubeDeploymentReplicasMismatch', {'deployment': 'catalog'}, 'customer-a-incidents'),
        ('probe-stands', 'ServiceDown', {'namespace': 'dev', 'instance': 'https://stand.invalid'}, 'test-incidents-it'),
        ('probe', 'ServiceDown', {'instance': 'https://app.invalid', 'service': 'catalog'}, 'customer-b-incidents'),
        ('k8s-node', 'KubeNodeNotReady', {'node': 'worker-lab'}, 'customer-a-incidents'),
        ('node', 'NodeHighCpu', {'instance': 'worker-lab:9100'}, 'customer-a-incidents'),
        ('longhorn', 'LonghornVolumeDegraded', {'node': 'storage-lab'}, 'customer-a-incidents'),
        ('storage-pvc', 'KubePersistentVolumeFillingUp', {'persistentvolumeclaim': 'data-store-0'}, 'customer-a-incidents'),
        ('storage-host', 'NodeFilesystemAlmostOutOfSpace', {'instance': 'disk-lab:9100'}, 'customer-a-incidents'),
        ('storage', 'LonghornInstanceManagerCpuRequest', {'service': 'longhorn'}, 'customer-a-incidents'),
        ('db', 'PostgresDown', {'server': 'pg-lab'}, 'cloud-incidents'),
        ('kafka', 'KafkaConsumerLag', {'topic': 'orders', 'consumergroup': 'core-lab'}, 'cloud-incidents'),
        ('other-namespace', 'ApplicationUnknownFailure', {'service': 'billing'}, 'cloud-incidents'),
        ('other', 'UnknownExporterFailure', {'namespace': None, 'service': 'exporter'}, 'customer-a-incidents'),
        ('unlabeled', 'KubePodMissingNamespace', {'namespace': None, 'pod': 'orphan'}, 'customer-a-incidents'),
    ]
    clusters = {'lis': 'customer-b', 'probe': 'customer-b', 'probe-stands': 'development-cluster', 'db': 'production-cluster',
        'kafka': 'infra-cluster', 'other-namespace': 'infra-cluster'}
    scenario_directory = os.environ.get('KEEP_LAB_SCENARIO_DIR')
    scenario_events, scenario_expectations = {}, {}
    if scenario_directory:
        scenario = Path(scenario_directory).resolve()
        assert scenario.is_relative_to((ROOT / '.lab-work').resolve()), 'Use local saved synthetic fixtures'
        saved_events = json.loads((scenario / 'live-events.json').read_text())
        saved_expectations = json.loads((scenario / 'live-incidents.json').read_text())
        scenario_expectations = {item['rule']: item for item in saved_expectations}
        events_by_fingerprint = {item['fingerprint']: item for item in saved_events}
        scenario_events = {rule: events_by_fingerprint[item['fingerprint']]
            for rule, item in scenario_expectations.items()}
        assert set(scenario_events) == {case[0] for case in cases}, 'Saved scenarios must cover every rule family'
    events, expectations = [], []
    prior = json.loads((RUN / 'live-incidents.json').read_text()) if (RUN / 'live-incidents.json').exists() else []
    original_ids = {item['incident_id'] for item in prior}
    for rule, alertname, extra, channel in cases:
        labels = {'alertname': alertname, 'cluster': clusters.get(rule, 'customer-a'),
            'namespace': 'app-prod' if rule == 'other-namespace' else 'prod', 'severity': 'warning',
            'level': 'business_critical', 'lab_run': PREFIX, **extra}
        labels = {k: v for k, v in labels.items() if v is not None}
        if scenario_events:
            labels = {**scenario_events[rule]['labels'], 'lab_run': PREFIX}
            channel = scenario_expectations[rule]['channel']
        fingerprint = PREFIX + '-' + rule
        events.append({'status': 'firing', 'fingerprint': fingerprint, 'labels': labels,
            'annotations': {'summary': 'Enterprise core verification: ' + rule, 'description': 'Lab-only engineer scenario ' + rule},
            'generatorURL': 'https://prometheus.lab.invalid/graph?expr=' + alertname,
            'startsAt': datetime.now(timezone.utc).isoformat()})
        expectations.append({'fingerprint': fingerprint, 'rule': rule, 'channel': channel,
            'team': scenario_expectations[rule]['team'] if scenario_expectations else 'it' if rule == 'probe-stands' else 'ops'})
    (RUN / 'live-events.json').write_text(json.dumps(events, indent=2))
    # Save exact synthetic inputs for browser journeys/replays; no credentials stored.
    keep('/alerts/event/prometheus', 'POST', {'alerts': events})
    def rows():
        return keep('/incidents?limit=50&sorting=-creation_time')['items']
    def new_rows(include_extra=False):
        return [row for row in rows() if (row.get('correlation') or {}).get('rule_id') in {case[0] for case in cases}
            and row.get('creation_time', '') >= datetime.strptime(RUN.name, '%Y%m%dT%H%M%SZ').isoformat()
            and (include_extra or not original_ids or row['id'] in original_ids)]
    found = eventually('all sixteen rule families create canonical incidents', lambda: (v if len(v) == 16 else None) if (v := new_rows()) else None)
    channels = json.loads((RUN / 'channels.json').read_text())
    enriched = []
    for expected in expectations:
        candidates = [row for row in found if row.get('correlation', {}).get('rule_id') == expected['rule']]
        check('exactly one canonical incident for ' + expected['rule'], len(candidates) == 1)
        incident = candidates[0]
        check('team routing for ' + expected['rule'], incident['team_id'] == expected['team'])
        channel_id = channels[expected['channel']]
        def post():
            feed = mm('/channels/' + channel_id + '/posts?per_page=100')
            candidates = [p for p in feed['posts'].values() if incident['id'] in json.dumps(p.get('props', {}))]
            return candidates[0] if len(candidates) == 1 else None
        delivered = eventually('one real Mattermost post for ' + expected['rule'], post)
        enriched.append({**expected, 'incident_id': incident['id'], 'post_id': delivered['id'], 'channel_id': channel_id,
            'revision': incident.get('lifecycle', {}).get('revision'), 'created_at': incident['creation_time']})
    (RUN / 'live-incidents.json').write_text(json.dumps(enriched, indent=2))
    # Different replicas of one workload stay one incident; a different service/team stays separate.
    extra = json.loads(json.dumps(events[2]))
    extra['fingerprint'] = PREFIX + '-workload-replica'
    extra['labels']['pod'] = 'catalog-abcde-fghij'
    keep('/alerts/event/prometheus', 'POST', {'alerts': [extra]})
    workload = next(row for row in enriched if row['rule'] == 'workload')
    eventually('replicas group into the same workload incident', lambda: keep('/incidents/' + workload['incident_id']).get('alerts_count') == 2)
    other = json.loads(json.dumps(extra)); other['fingerprint'] = PREFIX + '-other-service'; other['labels']['deployment'] = 'another-service'
    foreign = json.loads(json.dumps(extra)); foreign['fingerprint'] = PREFIX + '-foreign-team'
    foreign['labels']['cluster'] = scenario_events['probe-stands']['labels']['cluster'] if scenario_events else 'infra-cluster'
    foreign['labels']['namespace'] = 'internal-tools'
    keep('/alerts/event/prometheus', 'POST', {'alerts': [other, foreign]})
    eventually('another service and team create separate incidents', lambda: len([row for row in new_rows(True) if row.get('correlation', {}).get('rule_id') == 'workload']) == 3)
    check('workload incident retains only its replicas', keep('/incidents/' + workload['incident_id'])['alerts_count'] == 2)
    (RUN / 'live-checks.json').write_text(json.dumps({'passed': True, 'checks': checks, 'prefix': PREFIX}, indent=2))


if __name__ == '__main__':
    main()
