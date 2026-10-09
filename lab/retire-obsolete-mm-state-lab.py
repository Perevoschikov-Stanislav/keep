"""Retire absent local MM posts after channel recreation, preserving a SQLite backup."""
import importlib.util
import json
from pathlib import Path
from urllib.parse import urlsplit

spec = importlib.util.spec_from_file_location('prepare', Path(__file__).with_name('prepare-core-lab.py'))
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


def main():
    config = json.loads(prepare.kube('config', 'view', '--minify', '-o', 'json'))
    assert urlsplit(config['clusters'][0]['cluster']['server']).hostname in {'0.0.0.0', 'localhost', '127.0.0.1'}
    backup = '/state/backups/mm-channel-recreation-' + prepare.RUN.name + '.sqlite'
    code = """
import json,os,sqlite3,urllib.request,urllib.error
from pathlib import Path
from urllib.parse import urlsplit
conf=json.loads(Path(os.environ['BRIDGE_CONFIG_FILE']).read_text())
assert urlsplit(conf['mattermost_url']).hostname in {'mattermost','mattermost.keep-lab.svc'}, 'Only local MM'
channels={d['channel_id'] for d in conf['destinations'].values()}
db=sqlite3.connect('/state/transport.db');db.row_factory=sqlite3.Row
rows=db.execute('select * from delivery').fetchall()
obsolete=[row for row in rows if json.loads(row['envelope'])['channel_id'] not in channels]
assert not any(row['state']=='unknown' for row in obsolete), 'Uncertain effects require separate review'
extids={row['external_id'] for row in obsolete if row['external_id']}
def missing(identifier):
    try:
        req=urllib.request.Request(conf['mattermost_url']+'/api/v4/posts/'+identifier,
            headers={'Authorization':'Bearer '+os.environ['MM_BOT_TOKEN']})
        with urllib.request.urlopen(req,timeout=5): return False
    except urllib.error.HTTPError as error:
        assert error.code==404, 'Unexpected local post validation result'
        return True
bindings=[row for row in db.execute('select * from binding') if row['external_id'] in extids]
assert all(missing(row['external_id']) for row in bindings), 'Old post still exists; do not retire it'
service=[row for row in db.execute('select * from silence_post') if row['external_id'] and missing(row['external_id'])]
backup=Path(BACKUP)
assert not backup.exists(), 'Preserve the existing backup; choose a new run'
backup.parent.mkdir(parents=True,exist_ok=True)
with sqlite3.connect(backup) as target: db.backup(target)
backup.chmod(0o600)
with db:
    for table,records in [('binding',bindings),('delivery',obsolete),('silence_post',service)]:
        db.executemany('delete from '+table+' where id=?',[(row['id'],) for row in records])
print(json.dumps({'retired_bindings':len(bindings),'retired_deliveries':len(obsolete),
    'retired_service_posts':len(service),'preserved_bindings':db.execute('select count(*) from binding').fetchone()[0]}))
""".replace('BACKUP', repr(backup))
    result = json.loads(prepare.kube('exec', '-i', 'deployment/keep-notification-bridge', '--', 'python', '-', payload=code))
    import subprocess
    output = subprocess.run(prepare.KUBE + ['exec', 'deployment/keep-notification-bridge', '--', 'cat', backup], capture_output=True)
    assert output.returncode == 0, 'Read the preserved local backup'
    path = prepare.RUN / 'bridge.before-obsolete-cleanup.sqlite'
    assert not path.exists(), 'Do not replace an existing backup'
    path.write_bytes(output.stdout)
    path.chmod(0o600)
    (prepare.RUN / 'obsolete-bridge-state-retired.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
