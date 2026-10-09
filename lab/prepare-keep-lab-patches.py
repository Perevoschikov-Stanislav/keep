"""Generate local deployment patches from the checked-in lab configuration."""

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend-image", required=True)
    parser.add_argument("--frontend-image", required=True)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    lab = Path(__file__).resolve().parent
    output = args.output_dir or lab
    output.mkdir(parents=True, exist_ok=True)

    backend = json.loads((lab / "keep-backend.patch.json").read_text())
    container = backend["spec"]["template"]["spec"]["containers"][0]
    container.update(image=args.backend_image, imagePullPolicy="IfNotPresent")
    environment = {item["name"]: item for item in container["env"] if not item["name"].startswith("KEEP_ALERTMANAGER_")}
    environment["KEEP_TEAMS_CONFIG"] = {"name": "KEEP_TEAMS_CONFIG", "value": (lab / "team-policy.yaml").read_text()}
    settings = json.loads((lab / "alertmanager-reconciliation.json").read_text())
    for name, value in settings.items():
        if not name.startswith("KEEP_ALERTMANAGER_"):
            raise ValueError("Unexpected Alertmanager setting: " + name)
        value = json.dumps(value, separators=(",", ":")) if isinstance(value, (bool, dict, list)) else str(value)
        environment[name] = {"name": name, "value": value}
    container["env"] = list(environment.values())

    frontend = json.loads((lab / "keep-frontend.image.patch.json").read_text())
    frontend["spec"]["template"]["spec"]["containers"][0].update(image=args.frontend_image, imagePullPolicy="IfNotPresent")
    image_patch = {"spec": {"template": {"spec": {"containers": [{"name": "keep", "image": args.backend_image, "imagePullPolicy": "IfNotPresent"}]}}}}
    for filename, patch in (("keep-backend.patch.json", backend),
                            ("keep-backend.image.patch.json", image_patch),
                            ("keep-frontend.image.patch.json", frontend)):
        target = output / filename
        target.write_text(json.dumps(patch, indent=2) + "\n")
        print(target)


if __name__ == "__main__":
    main()
