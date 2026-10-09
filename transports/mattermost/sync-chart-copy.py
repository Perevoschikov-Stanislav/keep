"""Refresh the local historical chart copy from the fork's versioned transport."""

import hashlib
import json
import shutil
from pathlib import Path

import yaml


if __name__ == "__main__":
    source = Path(__file__).resolve().parent
    target = source.parents[2] / "mm-bridge"
    if not target.resolve().is_relative_to(source.parents[2].resolve()):
        raise SystemExit("Only the local keepfork/mm-bridge copy is supported")
    for name in ("bridge.py", "contract.json", "Dockerfile"):
        shutil.copyfile(source / name, target / "bridge" / name)
    shutil.copytree(source / "chart", target / "transport-chart", dirs_exist_ok=True)
    (target / "README-TRANSPORT.md").write_text((source / "README.md").read_text())
    # Replace only the keepMm block; the rest of the historical Keep chart is retained.
    path = target / "values.yaml"
    raw = path.read_text()
    tree = yaml.compose(raw)
    key, value = next((key, value) for key, value in tree.value if key.value == "keepMm")
    defaults = yaml.safe_load((source / "chart/values.yaml").read_text())
    block = {"keepMm": {"enabled": True, **defaults}}
    path.write_text(raw[:key.start_mark.index] + yaml.safe_dump(block, sort_keys=False) + "\n" + raw[value.end_mark.index:])
    template = (source / "chart/templates/bridge.yaml").read_text().replace(".Values.", ".Values.keepMm.")
    template = template.replace("{{ .Release.Name }}", "keep-mm-bridge")
    (target / "templates/keep-mm.yaml").write_text("{{- if .Values.keepMm.enabled }}\n" + template + "\n{{- end }}\n")
    (target / "templates/keep-mm-bot.yaml").write_text("{{/* Bot credential is an existing Secret, configured through keepMm.secretRefs.mattermost. */}}\n")
    # Obsolete domain test assertions must not report success for the transport.
    (target / "bridge/test_bridge.py").write_text('''"""Run the transport regression suite."""
import os
import runpy
from pathlib import Path

fork = Path(__file__).resolve().parents[2] / "fork"
os.chdir(fork)
runpy.run_path(str(fork / "lab/check-mm-bridge-k3d.py"), run_name="__main__")
''')
    manifest = {"canonical": str(source), "files": {name: hashlib.sha256((source / name).read_bytes()).hexdigest()
        for name in ("bridge.py", "contract.json", "Dockerfile")}}
    (target / "bridge/source-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print("Local chart copy refreshed")
