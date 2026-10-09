# Silences

A silence suppresses outgoing notifications, including alert/incident messages.
Events are stored, their canonical status is preserved, grouping continues,
and workflow actions marked `notification: false` continue to execute.
Repeated samples do not remove an active silence.

The UI registry supports creation, scheduling, editing and cancellation;
alerts and incidents can create rules. A responder manages rules only for
member teams. A viewer reads allowed rules. Admin has unrestricted access.
Rules use revisions, idempotency keys, command receipts and lifecycle audit.

## Documentation

- `contract-v1.md`, `contract-v1.schema.json`, `fixtures-v1.json`: protocol,
  selector semantics and synthetic examples.
- `integrations.md`: registered clients, operator proof, lifecycle events,
  subscriptions and recovery.
- `alertmanager.md`: bounded export/import, ownership and reconciliation.
- `migration.md`: transition from legacy Dismiss and Maintenance.
- `workflows/`: explicit notification action classification.

Silence state belongs to Keep. A transport forwards commands and events using
its configured permission ceiling; a message or channel is not operator proof.
Mattermost is one optional adapter. Run `lab/check-silences-contract.py` for
wire fixtures and `lab/check-silences-integration-k3d.py` for the local suite.
