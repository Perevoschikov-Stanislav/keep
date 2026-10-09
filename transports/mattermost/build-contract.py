"""Copy the reachable wire definitions; validate against Keep's canonical schemas."""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def wire(path, name):
    schema = json.loads(path.read_text())
    definitions = {}
    pending = [name]
    while pending:
        key = pending.pop()
        if key in definitions:
            continue
        definitions[key] = schema["$defs"][key]
        def visit(value):
            if isinstance(value, dict):
                if "$ref" in value:
                    pending.append(value["$ref"].removeprefix("#/$defs/"))
                for item in value.values():
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)
        visit(definitions[key])
    return {"$schema": schema["$schema"], "$defs": definitions, "$ref": "#/$defs/" + name}


if __name__ == "__main__":
    document = {"Notification": wire(ROOT / "keep/api/core/incident_contract_v1.schema.json", "Notification"),
        "SilenceEvent": wire(ROOT / "docs/fork/silences/contract-v1.schema.json", "LifecycleEvent")}
    Path(__file__).with_name("contract.json").write_text(json.dumps(document, indent=2) + "\n")
