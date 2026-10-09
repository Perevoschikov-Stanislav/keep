"""Offline bridge-copy inventory and reviewed Keep import; never call a transport."""

import argparse
import json
import os
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from keep.api.bl.incident_provisioning import Candidate
from keep.api.bl.legacy_incident_migration import LegacyIncidentMigration, export_snapshot, read_bridge_snapshot
from keep.api.core import db
from keep.api.core.dependencies import SINGLE_TENANT_UUID
from keep.api.core.incident_contract import ContractError, read_yaml, require


def save_private(path, value):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("export", "preview", "apply", "receipts", "restore-bindings"))
    parser.add_argument("--tenant", default=SINGLE_TENANT_UUID)
    parser.add_argument("--source-id")
    parser.add_argument("--state-db", type=Path, help="Standalone read-only SQLite backup, including committed WAL data")
    parser.add_argument("--export", type=Path, dest="export_path")
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--reviewed-preview", type=Path)
    parser.add_argument("--receipt-snapshot", type=Path)
    parser.add_argument("--output", type=Path, required=True, help="New private output file; existing files are never overwritten")
    parser.add_argument("--actor", default="iac-cli")
    args = parser.parse_args(argv)
    try:
        require(not args.output.exists(), "output", "choose a new path to retain the reviewed artifacts")
        service = LegacyIncidentMigration(args.tenant)
        with Session(db.engine) as session:
            if args.command in {"export", "preview", "receipts"} and session.get_bind().dialect.name == "postgresql":
                session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            if args.command == "export":
                if not args.state_db or not args.source_id:
                    parser.error("export requires --state-db and --source-id")
                result = export_snapshot(session, args.tenant, args.source_id, read_bridge_snapshot(args.state_db))
                summary = {"roots": len(result["roots"]), "source_digest": result["digest"]}
            elif args.command in {"preview", "apply"}:
                if not all((args.export_path, args.bundle, args.plan)):
                    parser.error("preview/apply require --export, --bundle and --plan")
                exported = json.loads(args.export_path.read_text())
                candidate = Candidate.from_file(args.bundle, args.tenant)
                plan = read_yaml(args.plan)
                if args.command == "preview":
                    result = service.preview(session, exported, candidate, plan)
                    summary = {**result["summary"], "preview_digest": result["preview_digest"]}
                else:
                    if not args.reviewed_preview or not args.state_db:
                        parser.error("apply requires --reviewed-preview and the same --state-db copy")
                    require(read_bridge_snapshot(args.state_db)["digest"] == exported["bridge_digest"], "bridge.source", "bridge copy changed")
                    reviewed = json.loads(args.reviewed_preview.read_text())
                    result = service.apply(session, exported, candidate, plan,
                        expected_preview_digest=reviewed["preview_digest"], actor=args.actor)
                    summary = {"result": result["result"], "imports": len(result["items"])}
            else:
                if not args.source_id:
                    parser.error("receipts/restore-bindings require --source-id")
                if args.command == "receipts":
                    result = service.receipts(session, args.source_id)
                    summary = {"imports": len(result["imports"]), "snapshot_digest": result["digest"]}
                else:
                    if not args.receipt_snapshot:
                        parser.error("restore-bindings requires --receipt-snapshot")
                    saved = json.loads(args.receipt_snapshot.read_text())
                    require(saved["tenant_id"] == args.tenant and saved["source_id"] == args.source_id, "migration.restore", "snapshot scope mismatch")
                    result = service.restore_bindings(session, args.source_id, expected_snapshot_digest=saved["digest"])
                    summary = result
        save_private(args.output, result)
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    except ContractError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, KeyError, ValueError, TypeError, SQLAlchemyError):
        print("Legacy migration failed; check artifact paths/digests and review the private preview.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
