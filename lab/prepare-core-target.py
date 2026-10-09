"""Prepare a target draft offline; read Enterprise IaC, write only inside the fork."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from uuid import UUID

import yaml

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get('KEEP_IAC_DIR', ROOT.parent / 'iac/keep'))
LOCAL_CONFIG = Path(os.environ.get('KEEP_LOCAL_CONFIG_ROOT', ROOT / '.lab-work/config'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cutover', required=True, help='Reviewed UTC RFC3339 start of the new sender')
    parser.add_argument('--active-ids', type=Path, help='Reviewed JSON list of active incident UUIDs to recreate')
    args = parser.parse_args()
    if not (SOURCE / 'values.yaml').is_file():
        parser.error('Set KEEP_IAC_DIR to the read-only source chart directory')
    from datetime import datetime
    assert re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', args.cutover), 'Use UTC with Z'
    datetime.fromisoformat(args.cutover.replace('Z', '+00:00'))
    selected = json.loads(args.active_ids.read_text()) if args.active_ids else []
    assert isinstance(selected, list) and len(selected) <= 1000
    selected = sorted({str(UUID(value)) for value in selected})
    base = LOCAL_CONFIG / 'incident-core.lab'
    if not base.is_dir():
        base = ROOT / 'config/incident-core.lab'
    out = LOCAL_CONFIG / 'incident-core.target'
    out.mkdir(parents=True, exist_ok=True)
    for name in ('mappings', 'extraction', 'workflows'):
        shutil.copytree(base / name, out / name, dirs_exist_ok=True)
    shutil.copyfile(base / 'teams.yaml', out / 'teams.yaml')
    values = yaml.safe_load((SOURCE / 'values.yaml').read_text())
    source = values['keepMm']
    bundle = copy.deepcopy(yaml.safe_load((base / 'bundle.yaml').read_text()))
    bundle.update(id='incident-core', revision='core-target-20261008',
        keep_url='https://' + values['ingress']['host'], adoptions=[])
    bundle['access']['sha256'] = hashlib.sha256((out / 'teams.yaml').read_bytes()).hexdigest()
    assert len(bundle['routes']) == len(source['routes']), 'Source routes changed; review translation'
    destinations = {item['id']: item for item in bundle['destinations']}
    channels = {}
    for route, original in zip(bundle['routes'], source['routes']):
        fence = 'incident.created_at >= ' + json.dumps(args.cutover)
        if selected:
            fence = '(' + fence + ' || incident.id in ' + json.dumps(selected) + ')'
        route['match'], count = re.subn(r'incident\.created_at >= "[^"]+"', lambda _: fence, route['match'])
        assert count == 1, 'Expected one lab history fence per route'
        for ref in route['destination_refs']:
            assert ref == destinations[ref]['team_id'] + '-' + original['name'], 'Source route order changed'
            destinations[ref]['options']['channel_id'] = original['channel']
            channels[ref] = {'team_id': destinations[ref]['team_id'], 'channel_id': original['channel'], 'silence_service_posts': True}
    (out / 'bundle.yaml').write_text(yaml.safe_dump(bundle, allow_unicode=True, sort_keys=False))
    bridge = yaml.safe_load((base / 'bridge-values.yaml').read_text())
    bridge['image']['pullPolicy'] = 'IfNotPresent'
    bridge['configuration'].update(keep_ui_url=bundle['keep_url'], mattermost_url=source['mattermostUrl'],
        timeout_seconds=10, destinations=channels)
    (out / 'bridge-values.yaml').write_text(yaml.safe_dump(bridge, allow_unicode=True, sort_keys=False))
    (out / 'cutover.yaml').write_text(yaml.safe_dump({'starts_at': args.cutover, 'recreate_active_incident_ids': selected}, sort_keys=False))
    manifest = {'source': str(SOURCE), 'values_sha256': hashlib.sha256((SOURCE / 'values.yaml').read_bytes()).hexdigest(),
        'routes': source['routes'], 'selected_active_incident_ids': selected}
    (out / 'source-routes.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'target': str(out), 'routes': len(bundle['routes']), 'channels': len(set(v['channel_id'] for v in channels.values())),
        'selected_active_incidents': len(selected), 'cutover': args.cutover}))


if __name__ == '__main__':
    main()
