"""Translate the read-only Enterprise IaC into an explicit, local-only core bundle."""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import urllib.error
import urllib.request

import yaml

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get('KEEP_IAC_DIR', ROOT.parent / 'iac/keep'))
LOCAL_CONFIG = Path(os.environ.get('KEEP_LOCAL_CONFIG_ROOT', ROOT / '.lab-work/config'))
OUT = LOCAL_CONFIG / 'incident-core.lab'
RUN = Path(os.environ.get('KEEP_CORE_RUN_DIR') or (ROOT / '.lab-work/incident-core-upgrade/CURRENT').read_text().strip())
assert RUN.resolve().is_relative_to((ROOT / '.lab-work').resolve()), 'Artifacts must stay inside the fork'
KUBE = ['kubectl', '--context', 'k3d-local', '--cache-dir=' + str(ROOT / '.lab-work/kube-cache'), '-n', 'keep-lab']


def kube(*args, payload=None):
    result = subprocess.run(KUBE + list(args), input=payload, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError('Local kubectl operation failed; no credentials logged')
    return result.stdout


def artifact(relative):
    return {'path': relative, 'sha256': hashlib.sha256((OUT / relative).read_bytes()).hexdigest()}


def main():
    if not (SOURCE / 'values.yaml').is_file():
        raise RuntimeError('Set KEEP_IAC_DIR to the read-only source chart directory')
    config = json.loads(kube('config', 'view', '--minify', '-o', 'json'))
    from urllib.parse import urlsplit
    assert urlsplit(config['clusters'][0]['cluster']['server']).hostname in {'0.0.0.0', '127.0.0.1', 'localhost'}
    OUT.mkdir(parents=True, exist_ok=True)
    legacy = LOCAL_CONFIG / 'incident-legacy.example'
    if not legacy.is_dir():
        legacy = ROOT / 'config/incident-legacy.example'
    for name in ('mappings', 'extraction'):
        shutil.copytree(legacy / name, OUT / name, dirs_exist_ok=True)
    teams = yaml.safe_load((ROOT / 'lab/team-policy.yaml').read_text())
    teams['teams'].append({'id': 'quarantine', 'groups': [], 'zones': ['UNASSIGNED'], 'visible_to': []})
    (OUT / 'teams.yaml').write_text(yaml.safe_dump(teams, allow_unicode=True, sort_keys=False))
    source_values = yaml.safe_load((SOURCE / 'values.yaml').read_text())['keepMm']
    old = json.loads((RUN / 'keep-mm-bridge.before.json').read_text())['spec']['template']['spec']['containers'][0]
    old_env = {item['name']: item.get('value') for item in old.get('env', [])}
    secret = json.loads(kube('get', 'secret', 'keep-mm-bridge', '-o', 'json'))
    token = base64.b64decode(secret['data']['MM_BOT_TOKEN']).decode()

    def mm(method, path, data=None):
        request = urllib.request.Request('http://localhost:8065/api/v4' + path,
            data=json.dumps(data).encode() if data is not None else None, method=method,
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)

    team = mm('GET', '/channels/' + old_env['MM_CHANNEL_OPS'])['team_id']
    channel_ids = {}
    for route in source_values['routes']:
        name = 'core-' + route['name']
        try:
            channel = mm('GET', '/teams/' + team + '/channels/name/' + name)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise RuntimeError('Local Mattermost channel lookup failed') from None
            channel = mm('POST', '/channels', {'team_id': team, 'name': name,
                'display_name': 'Core lab · ' + route['name'], 'type': 'P'})
        channel_ids[route['name']] = channel['id']
    (RUN / 'channels.json').write_text(json.dumps(channel_ids, indent=2))
    bundle = {'api_version': 'keep.incidents/v1', 'kind': 'IncidentPolicies', 'id': 'incident-core-lab',
        'tenant_id': 'keep', 'revision': 'incident-core-20261007', 'keep_url': 'http://localhost:8000',
        'access': artifact('teams.yaml'),
        'runtime_ownership': [{'team_id': team, 'domain': 'keep',
            'notifications': 'keep' if team != 'quarantine' else 'disabled', 'legacy_snooze': 'disabled'}
            for team in ('ops', 'it', 'quarantine')] + [{'team_id': None, 'domain': 'disabled',
                'notifications': 'disabled', 'legacy_snooze': 'disabled'}]}
    for section in ('mappings', 'extraction'):
        bundle[section] = [{'id': path.stem, 'artifact': artifact(str(path.relative_to(OUT)))}
            for path in sorted((OUT / section).glob('*.yaml'))]
    inventory_code = '''
import json
from sqlmodel import Session,select
from keep.api.core import db
from keep.api.bl.incident_provisioning import MODELS,current_values,digest
out=[]
with Session(db.engine) as s:
 for kind,model in MODELS.items():
  for row in s.exec(select(model).where(model.tenant_id=='keep')).all():
   out.append(dict(kind=kind,id=str(row.id),name=row.name,digest=digest(current_values(kind,row)),deleted=getattr(row,'is_deleted',False)))
print('INVENTORY_JSON='+json.dumps(out))
'''
    raw = kube('exec', '-i', 'deployment/keep-backend', '-c', 'keep', '--', 'python', '-', payload=inventory_code)
    inventory = json.loads(next(line.split('=', 1)[1] for line in raw.splitlines() if line.startswith('INVENTORY_JSON=')))
    (RUN / 'resource-inventory.before.json').write_text(json.dumps(inventory, indent=2))
    bundle['workflows'] = []
    for path in (legacy / 'workflows').glob('*.yaml'):
        document = yaml.safe_load(path.read_text())
        document['workflow']['disabled'] = True
        relative = 'workflows/' + path.name
        (OUT / 'workflows').mkdir(exist_ok=True)
        (OUT / relative).write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False))
        bundle['workflows'].append({'id': path.stem, 'artifact': artifact(relative)})
    bundle['adoptions'] = []
    for section in ('mappings', 'extraction', 'workflows'):
        for item in bundle[section]:
            existing = next((row for row in inventory if row['kind'] == section and row['name'] == item['id'] and not row['deleted']), None)
            if existing:
                bundle['adoptions'].append({'kind': section, 'id': item['id'], 'target_id': existing['id'],
                    'expected_resource_digest': existing['digest']})
    def field(name, sources):
        return {'name': name, 'sources': sources, 'missing': {'mode': 'mark_unknown'}}
    base_fields = [field('cluster', ['cluster', 'labels.cluster']), field('environment', ['env_kind']),
        field('namespace', ['namespace', 'labels.namespace']), field('kind', ['family']),
        field('workload', ['svc']), field('service', ['svc']), field('routing_level', ['level', 'labels.level'])]
    bundle['normalization'] = [{'id': 'core', 'team_ids': ['ops', 'it', 'quarantine'], 'priority': 100,
        'match': 'true', 'fields': base_fields + [field('resource', ['labels.case', 'labels.upstream',
            'labels.pod', 'labels.persistentvolumeclaim', 'labels.node', 'labels.instance',
            'labels.server', 'labels.consumergroup', 'labels.topic', 'alertname'])]}]
    presentation = {'id': 'engineer', 'title': '{{ normalized.cluster }} · {{ normalized.namespace }} · {{ normalized.service }}: {{ normalized.kind }}',
        'description': '{{ incident.status }} · {{ normalized.kind }} · {{ normalized.resource }}',
        'fields': [{'path': path, 'label': label, 'order': i} for i, (path, label) in enumerate([
            ('incident.status', 'Status'), ('incident.assignee', 'Assignee'), ('normalized.cluster', 'Cluster'),
            ('normalized.namespace', 'Namespace'), ('normalized.service', 'Service'), ('normalized.resource', 'Object'),
            ('incident.alerts_count', 'Alerts'), ('normalized.routing_level', 'Routing level'),
            ('incident.severity', 'Severity'), ('incident.flapping.active', 'Flapping')])],
        'actions': [{'command': action, 'label': label} for action, label in [('ack', 'Acknowledge'),
            ('assign', 'Assign to me'),
            ('resolve', 'Resolve'), ('silence', 'Silence')]],
        'links': [{'label': 'Incident in Keep', 'url_template': '{{ keep_url }}/incidents/{{ incident.id }}'}],
        'severity_colors': {'info': '#707070', 'low': '#707070', 'medium': '#f0ad4e', 'high': '#d00000', 'critical': '#d00000'}}
    bundle['presentations'] = [presentation]
    bundle['lifecycle'] = [{'id': 'core', 'resolve_on': 'all_resolved', 'reopen': {'mode': 'reopen',
        'within_seconds': source_values['quietMinutes'] * 60, 'ack': 'reset', 'assignee': 'preserve'},
        'flapping': {'enabled': True, 'window_seconds': source_values['flapMinutes'] * 60,
            'transition_threshold': 2, 'reset_after_seconds': source_values['flapMinutes'] * 60},
        'clock': 'receive_time', 'late_event_policy': 'history_only'}]
    source_rules = yaml.safe_load((SOURCE / 'config/rules/correlation.yaml').read_text())
    bundle['correlation_overlap'] = 'first_match'
    bundle['correlation'] = []
    for index, rule in enumerate(source_rules['rules']):
        fields = [name for name in rule['group_by'] if name != 'zone']
        bundle['correlation'].append({'id': rule['name'], 'team_ids': ['ops', 'it', 'quarantine'],
            'priority': 1000 - index, 'match': rule['cel'], 'group_by': fields, 'required_fields': fields,
            'missing_required': 'separate_alert', 'window_seconds': source_rules['defaults']['timeframe'],
            'threshold': 1, 'lifecycle_ref': 'core', 'presentation_ref': 'engineer'})
    # Empty Enterprise escalation chains stay empty. No invented on-call recipients.
    assert not any(source_values['escalation'].values()), 'Translate newly configured escalation explicitly'
    delivery = {'timeout_seconds': 12, 'retry': {'max_attempts': 6, 'initial_backoff_seconds': 2,
        'max_backoff_seconds': 60, 'multiplier': 2}, 'rate_limit': {'per_second': 10, 'burst': 20},
        'debounce_seconds': source_values['groupWaitSeconds']}
    bundle['transports'] = [{'id': 'mattermost-core', 'kind': 'mattermost', 'adapter_ref': 'mattermost-bridge-v1',
        'endpoint': 'http://keep-notification-bridge:8080', 'auth_ref': 'env:BRIDGE_TRANSPORT_TOKEN',
        'callback_client_ref': 'mattermost-core-recovery', 'capabilities': {'update': True, 'actions': False, 'receipts': True},
        'delivery': delivery}, {'id': 'silence-events', 'kind': 'http_json', 'adapter_ref': 'http-json-v1',
        'endpoint': 'http://keep-notification-bridge:8080', 'auth_ref': 'env:BRIDGE_TRANSPORT_TOKEN',
        'capabilities': {'update': False, 'actions': False, 'receipts': False}, 'delivery': {**delivery, 'debounce_seconds': 0}}]
    bundle['destinations'], bundle['routes'] = [], []
    bridge_destinations = {}
    from datetime import datetime
    cutoff = datetime.strptime(RUN.name, '%Y%m%dT%H%M%SZ').isoformat() + 'Z'
    cluster_aliases = {}
    for row in yaml.safe_load((OUT / 'mappings/zones.yaml').read_text()).get('rows', []):
        group = re.fullmatch(r'\^\(([\w|.-]+)\)\$', row.get('cluster', ''))
        if group:
            aliases = group[1].split('|')
            cluster_aliases.update({alias: aliases for alias in aliases})
    for index, route in enumerate(source_values['routes']):
        mask = route['match']
        route_teams = ['ops'] if mask.startswith('OPS/') else ['ops', 'it']
        destinations = []
        conditions = []
        if mask != '*':
            _, install, family = mask.split('/')
            cluster, *namespace = install.split(' ', 1)
            clusters = cluster_aliases.get(cluster, [cluster])
            conditions.append('normalized.cluster in ' + json.dumps(clusters))
            if namespace:
                conditions.append('normalized.namespace == ' + json.dumps(namespace[0]))
            if family != '*':
                conditions.append('normalized.kind == ' + json.dumps(family))
        conditions.append('incident.created_at >= ' + json.dumps(cutoff))
        for team_id in route_teams:
            identifier = team_id + '-' + route['name']
            destinations.append(identifier)
            if identifier not in bridge_destinations:
                bundle['destinations'].append({'id': identifier, 'team_id': team_id, 'transport_ref': 'mattermost-core',
                    'options': {'channel_id': channel_ids[route['name']]}})
            bridge_destinations[identifier] = {'team_id': team_id, 'channel_id': channel_ids[route['name']], 'silence_service_posts': True}
        bundle['routes'].append({'id': 'route-' + str(index), 'team_ids': route_teams, 'priority': 1000 - index,
            'match': ' && '.join(conditions), 'event_types': ['incident.' + name for name in
                ('created', 'updated', 'acknowledged', 'resolved', 'reopened', 'escalated', 'reminder')],
            'destination_refs': destinations, 'presentation_ref': 'engineer', 'delivery_mode': 'upsert',
            'update_fallback': 'append', 'actions_fallback': 'keep_link'})
    for team_id in ('ops', 'it'):
        bundle['destinations'].append({'id': team_id + '-silences', 'team_id': team_id,
            'transport_ref': 'silence-events', 'options': {'path': '/events'}})
    bundle['subscribers'] = [{'id': 'silence-projection', 'team_ids': ['ops', 'it'],
        'event_types': ['silence.' + name for name in ('created', 'updated', 'activated', 'cancelled', 'expired')],
        'destination_refs': ['ops-silences', 'it-silences']}]
    bundle['service_clients'] = [{'id': 'mattermost-core-recovery', 'auth_ref': 'env:KEEP_BRIDGE_SERVICE_TOKEN',
        'origin': 'notification-recovery', 'team_ids': ['ops', 'it'],
        'scopes': ['read:incident', 'read:silence', 'update:notification'], 'proof_profile_refs': []}]
    bundle['dispatch'] = {'scan_interval_seconds': 2, 'batch_size': 100, 'lease_seconds': 60, 'snapshot_page_size': 100}
    (OUT / 'bundle.yaml').write_text(yaml.safe_dump(bundle, allow_unicode=True, sort_keys=False))
    chart = yaml.safe_load((ROOT / 'transports/mattermost/chart/values.yaml').read_text())
    chart['image'].update(repository='keep-mm-bridge', tag='transport-v1', pullPolicy='Never')
    chart['configuration'].update(tenant_id='keep', transport_ref='mattermost-core', keep_ui_url='http://localhost:8000',
        mattermost_url='http://mattermost:8065', timeout_seconds=2, destinations=bridge_destinations)
    chart['secretRefs'].update(transport={'name': 'keep-core-bridge', 'key': 'transport'},
        service={'name': 'keep-core-bridge', 'key': 'service'}, mattermost={'name': 'keep-mm-bridge', 'key': 'MM_BOT_TOKEN'})
    (OUT / 'bridge-values.yaml').write_text(yaml.safe_dump(chart, allow_unicode=True, sort_keys=False))
    print(json.dumps({'bundle': str(OUT / 'bundle.yaml'), 'correlation_rules': len(bundle['correlation']),
        'routes': len(bundle['routes']), 'channels': len(channel_ids), 'history_fence': cutoff}))


if __name__ == '__main__':
    main()
