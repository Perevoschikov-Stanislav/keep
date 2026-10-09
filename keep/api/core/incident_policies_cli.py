"""python -m keep.api.core.incident_policies_cli validate|preview|apply|status."""

import argparse
import json
import sys

from keep.api.bl.incident_provisioning import Candidate, IncidentProvisioning
from keep.api.core.dependencies import SINGLE_TENANT_UUID
from keep.api.core.incident_contract import ContractError, read_yaml


def main():
    parser = argparse.ArgumentParser(description="Validate, preview and atomically apply incident IaC")
    parser.add_argument("command", choices=("validate", "preview", "apply", "status"))
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--bundle")
    source.add_argument("--restore-generation", type=int)
    parser.add_argument("--deletions-file", help="Reviewed YAML deletion list for a saved-version restore")
    parser.add_argument("--tenant", default=SINGLE_TENANT_UUID, help="Trusted deployment tenant")
    parser.add_argument("--expected-active-digest", help="Previous digest, or 'none' for first apply")
    parser.add_argument("--expected-candidate-digest")
    parser.add_argument("--expected-preview-digest")
    parser.add_argument("--actor", default="iac-cli")
    args = parser.parse_args()
    try:
        service = IncidentProvisioning(args.tenant)
        if args.command == "status":
            result = service.status()
        else:
            if not args.bundle and args.restore_generation is None:
                parser.error("--bundle or --restore-generation is required")
            if args.deletions_file and args.restore_generation is None:
                parser.error("--deletions-file requires --restore-generation")
            deletions = read_yaml(args.deletions_file) if args.deletions_file else None
            checked = service.restore_candidate(args.restore_generation, deletions=deletions) if args.restore_generation is not None else Candidate.from_file(args.bundle, args.tenant)
            if args.command == "validate":
                result = {"valid": True, "candidate_digest": checked.digest, "revision": checked.bundle["revision"]}
            elif args.command == "preview":
                result = service.preview(checked)
            else:
                if not all((args.expected_active_digest, args.expected_candidate_digest, args.expected_preview_digest)):
                    parser.error("apply requires all three expected digests from preview")
                result = service.apply(checked,
                    expected_active_digest=None if args.expected_active_digest == "none" else args.expected_active_digest,
                    expected_candidate_digest=args.expected_candidate_digest, expected_preview_digest=args.expected_preview_digest,
                    actor=args.actor)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except ContractError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
