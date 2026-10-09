"""Activate the saved, reviewed local preview with exactly one notification sender."""
import importlib.util
import json
from pathlib import Path
import subprocess

spec = importlib.util.spec_from_file_location('update', Path(__file__).with_name('update-core-lab.py'))
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)
ROOT, RUN, OUT, KUBE = update.ROOT, update.RUN, update.OUT, update.KUBE


def main():
    preview = json.loads((RUN / 'policy-preview.json').read_text())
    assert preview['active_digest'] is None and preview['generation'] == 0, 'Only first lab cutover is supported'
    assert all(change['operation'] in {'create', 'adopt'} for change in preview['changes'])
    print('kubectl --context k3d-local -n keep-lab scale deployment keep-mm-bridge --replicas=0', flush=True)
    update.prepare.kube('scale', 'deployment', 'keep-mm-bridge', '--replicas=0')
    command = ['helm', '--kube-context', 'k3d-local', '-n', 'keep-lab', 'upgrade', '--install',
        'keep-notification-bridge', str(ROOT / 'transports/mattermost/chart'), '-f', str(OUT / 'bridge-values.yaml'),
        '--wait', '--timeout', '60s']
    print(' '.join(command), flush=True)
    result = subprocess.run(command, capture_output=True, text=True)
    (RUN / 'bridge-rollout.log').write_text(result.stdout + result.stderr)
    assert result.returncode == 0, 'Transport rollout failed; policy remains inactive'
    command = KUBE + ['exec', 'deployment/keep-backend', '-c', 'keep', '--', 'python', '-m',
        'keep.api.core.incident_policies_cli', 'apply', '--tenant', 'keep', '--bundle', '/configuration/incident-core/bundle.yaml',
        '--expected-active-digest', 'none', '--expected-candidate-digest', preview['candidate_digest'],
        '--expected-preview-digest', preview['preview_digest'], '--actor', 'local-lab-cutover']
    print('Activate validated IncidentPolicies in k3d-local/keep-lab', flush=True)
    result = subprocess.run(command, capture_output=True, text=True)
    (RUN / 'policy-apply.json').write_text(result.stdout)
    (RUN / 'policy-apply.stderr').write_text(result.stderr)
    assert result.returncode == 0, 'Policy apply failed; inspect saved error and restore old sender if needed'
    applied = json.loads(result.stdout)
    print(json.dumps({'result': applied.get('result'), 'generation': applied.get('generation'), 'changes': len(applied.get('changes', []))}))


if __name__ == '__main__':
    main()
