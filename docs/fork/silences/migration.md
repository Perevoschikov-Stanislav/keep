# Legacy silence migration

Legacy Dismiss changes alert status through enrichments. Legacy Maintenance
can drop events before storage. The silence registry keeps events and canonical
status, suppressing only notification actions.

## Procedure

1. Preserve a consistent database snapshot and inventory. Run schema migrations
   for the selected backend image. Use a database copy for the initial check.
2. Configure the TeamPolicy and classify historical owners. Run
   `python -m keep.identitymanager.team_backfill` without `--apply` first.
   Only the zone stored on a historical event determines ownership; current
   fingerprint enrichment is not evidence of its historical owner.
3. Obtain the read-only silence inventory:

   ```bash
   python3 -B lab/migrate-legacy-silences.py --dry-run --tenant-id keep --report .lab-work/silences-migration/inventory.json
   ```

4. Review ambiguous owners, status overrides and Maintenance filters. Prepare
   an explicit plan mapping each Maintenance ID to configured teams and a
   canonical CEL expression. `source` is an array in the canonical alert and
   severity is numeric. Unknown or unreviewed selectors are rejected.
5. Mark each outgoing notification workflow action `notification: true`;
   other automatic actions remain enabled. Review partial coverage and the
   resulting payload before activating the new path.
6. Apply the reviewed inventory and team plan in the target environment:

   ```bash
   python3 -B lab/migrate-legacy-silences.py --apply --tenant-id keep --reviewed-inventory .lab-work/silences-migration/inventory.json --maintenance-team-plan .lab-work/silences-migration/maintenance-plan.json --report .lab-work/silences-migration/applied.json
   ```

   A changed source requires a new inventory and review. Keep the inventory,
   plan, result IDs and database snapshot in local storage, outside Git.
7. After comparing rules, owners and coverage, set
   `KEEP_MAINTENANCE_DESTRUCTIVE_DROP=false` for all backend workers. This stops
   legacy event dropping and status recovery and rejects creation/update via
   the old Maintenance API. Ensure one notification sender per owned scope.
8. Check role/team isolation, repeated samples, silence cancel/expiry, ordinary
   automation and notification recovery. The migration preserves audit and
   canonical incident IDs and manual fields.

Cancelling imported rules does not restore removed legacy enrichments. Recovery
requires the saved inventory and snapshot, while retaining newer events and
audit. A SQL status update alone is not a recovery procedure.
