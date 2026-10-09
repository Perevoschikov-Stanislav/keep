#!/usr/bin/env python3
"""CLI utility to inventory and migrate legacy Dismiss and Maintenance rules to Silences.

Usage:
    python lab/migrate-legacy-silences.py [--dry-run] [--tenant-id TENANT] [--report REPORT_PATH]
    python lab/migrate-legacy-silences.py --apply --reviewed-inventory REPORT_PATH [--tenant-id TENANT] [--skip-maintenance] [--report OUTPUT_PATH]
"""

import argparse
import json
import sys
from pathlib import Path

# Add project root to sys.path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from keep.api.core.db import get_session_sync
from keep.api.bl.silences_migration_bl import SilencesMigrationBL


def main():
    parser = argparse.ArgumentParser(
        description="Audit and migrate legacy Dismiss and Maintenance rules to the Silences registry."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Perform read-only inventory without modifying database (default).",
    )
    group.add_argument(
        "--apply",
        action="store_true",
        help="Apply migration idempotently to database.",
    )
    parser.add_argument(
        "--tenant-id",
        type=str,
        default=None,
        help="Target a specific tenant ID (default: all tenants).",
    )
    parser.add_argument(
        "--skip-maintenance",
        action="store_true",
        help="Skip migrating maintenance window rules.",
    )
    parser.add_argument(
        "--maintenance-team-plan",
        type=Path,
        help='Reviewed JSON: {"tenant": {"rule-id": {"teams": ["team-id", null], "cel": "true"}}}. Null means unassigned only.',
    )
    parser.add_argument(
        "--reviewed-inventory",
        type=Path,
        help="Previously reviewed dry-run report. Required for --apply; changed sources are rejected.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Optional path to output the full JSON report.",
    )

    args = parser.parse_args()
    is_apply = args.apply
    reviewed_inventory = None
    if is_apply:
        if args.reviewed_inventory is None:
            parser.error("--apply requires --reviewed-inventory from a reviewed dry-run")
        if args.report and args.report.resolve() == args.reviewed_inventory.resolve():
            parser.error("Write the apply report to a new path to preserve the reviewed inventory")
        saved = json.loads(args.reviewed_inventory.read_text())
        reviewed_inventory = saved.get("inventory") if isinstance(saved, dict) else None
        if not isinstance(reviewed_inventory, dict) or not isinstance(reviewed_inventory.get("source_hash"), str):
            parser.error("Reviewed inventory must be a dry-run report with inventory.source_hash")
        if reviewed_inventory.get("tenant_id") != args.tenant_id:
            parser.error("Use the same --tenant-id as the reviewed inventory")
    team_plan = None
    if args.maintenance_team_plan:
        team_plan = json.loads(args.maintenance_team_plan.read_text())
        if not isinstance(team_plan, dict):
            parser.error("Maintenance team plan must be a JSON object")

    session = get_session_sync()
    migration_bl = SilencesMigrationBL(session)

    try:
        print("=" * 70)
        print("  Keep Silences Legacy Migration & Audit")
        print("=" * 70)
        mode = "APPLY (WRITING CHANGES)" if is_apply else "DRY-RUN (READ ONLY)"
        print(f"Mode: {mode}")
        if args.tenant_id:
            print(f"Tenant: {args.tenant_id}")
        else:
            print("Tenant: all tenants")
        print("-" * 70)

        inventory_report = migration_bl.inventory(tenant_id=args.tenant_id)
        d_stats = inventory_report["dismissals"]
        m_stats = inventory_report["maintenance_rules"]
        admin_actions = inventory_report["admin_action_required"]

        print("\n[1] Legacy Dismissals Inventory:")
        print(f"  • Total Dismissed Enrichments:  {d_stats['total']}")
        print(f"  • Active (Migratable):          {d_stats['active_migratable']}")
        print(f"  • Expired (Will Not Migrate):   {d_stats['expired']}")
        print(f"  • Indefinite (Forever):         {d_stats['indefinite']}")
        print(f"  • With status=suppressed:       {d_stats['with_suppressed_override']}")
        print(f"  • With disposable flags:        {d_stats['with_disposable_flags']}")

        print("\n[2] Legacy Maintenance Windows Inventory:")
        print(f"  • Total Rules:                  {m_stats['total']}")
        print(f"  • Active (Migratable):          {m_stats['active_migratable']}")
        print(f"  • Expired / Disabled:           {m_stats['expired_or_disabled']}")
        print(f"  • Unassigned Team Rules:        {m_stats['unassigned_team_rules']}")

        if admin_actions:
            print("\n[!] Administrator Action Required:")
            print(f"  Found {len(admin_actions)} rule(s) requiring admin review:")
            for action in admin_actions:
                target = action.get("rule_id", action.get("fingerprint", "unknown"))
                print(f"    - [{action['type']}] {target}: {action['message']}")
            print("  Note: Maintenance rules are NOT automatically")
            print("  assigned to teams. Supply teams and reviewed CEL in --maintenance-team-plan.")
            print("  team_id=null covers unassigned objects only. Unknown status overrides are retained.")

        output_data = {"inventory": inventory_report}

        if is_apply:
            print("\n[3] Applying Migration:")
            apply_result = migration_bl.apply(
                tenant_id=args.tenant_id,
                include_maintenance=not args.skip_maintenance,
                inventory_report=reviewed_inventory,
                maintenance_teams=team_plan,
            )
            output_data["apply_result"] = apply_result

            print(f"  • Dismissals Migrated:          {apply_result['dismissals_migrated']}")
            print(f"  • Dismissals Skipped (Present): {apply_result['dismissals_skipped_already_present']}")
            print(f"  • Maintenance Rules Migrated:   {apply_result['maintenance_rules_migrated']}")
            print(f"  • Maintenance Skipped (Present):{apply_result['maintenance_rules_skipped_already_present']}")
            print(f"  • Suppressed Overrides Cleared: {apply_result['suppressed_overrides_cleared']}")
            print(f"  • Disposable Flags Cleaned:     {apply_result['disposable_flags_cleaned']}")
            print(f"  • Created Silence Records:      {len(apply_result['created_silence_ids'])}")
            print(f"  • Dismissals Pending Review:    {apply_result['dismissals_pending_review']}")
            print(f"  • Maintenance Pending Review:   {apply_result['maintenance_rules_pending_review']}")
            print("\n  Migration successfully applied and committed.")
        else:
            print("\n[3] Dry-Run Completed:")
            print("  No changes were made to the database.")
            print("  To execute migration, re-run with --apply.")

        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            with open(args.report, "w", encoding="utf-8") as f:
                json.dump(output_data, f, indent=2, ensure_ascii=False)
            print(f"\nReport written to: {args.report}")

        print("=" * 70)
    finally:
        session.close()


if __name__ == "__main__":
    main()
