"""Check real API/audit responses exported by test_silences_api_fork against v1."""

import argparse
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("samples", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "docs/fork/silences/contract-v1.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    samples = json.loads(args.samples.read_text())
    for index, sample in enumerate(samples):
        validator = Draft202012Validator({"$schema": schema["$schema"], "$defs": schema["$defs"],
            "$ref": f"#/$defs/{sample['schema']}"}, format_checker=FormatChecker())
        errors = list(validator.iter_errors(sample["body"]))
        if errors:
            paths = [".".join(str(part) for part in error.absolute_path) for error in errors]
            raise SystemExit(f"Invalid runtime response {index} ({sample['schema']}): {', '.join(paths)}")
    print(f"Validated {len(samples)} actual API/audit responses against v1.")


if __name__ == "__main__":
    main()
