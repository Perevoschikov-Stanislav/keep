# Incident runtime

Keep owns incident composition, lifecycle, assignment, silences and automation.
IaC owns team access, mapping/extraction, grouping policies, presentations,
routing, contacts and transport settings. Adapters deliver ready projections
and retain external bindings and delivery receipts.

## Documentation

| Document | Subject |
| --- | --- |
| `contract-v1.md`, `contract-v1.schema.json` | Bundle and notification wire contracts |
| `fixtures-v1.json`, `examples/` | Synthetic protocol and policy fixtures |
| `parameters-v1.md` | Generated field inventory |
| `provisioning.md` | Validate, preview, apply, adoption and drift |
| `normalization.md` | Enrichment, derived fields and presentations |
| `correlation.md` | Team scoped composition and typed grouping keys |
| `lifecycle.md` | ACK, resolve, reopen, episodes and flapping |
| `automation.md` | Deadlines, escalation, reminders and workflows |
| `notifications.md` | Routes, delivery queue, receipts and operator commands |

## Configuration

Set `KEEP_INCIDENT_POLICIES_CONFIG_FILE` to the mounted bundle. Team access and
managed resources publish atomically. Apply requires the active, candidate
and preview digests of the target database. Invalid input preserves the active
snapshot. Human actions use actual roles and team ownership; channel membership
and adapter credentials do not grant operator permissions.

Run `python3 -B lab/check-incident-contract.py` for contract fixtures and
`python3 -B lab/check-incident-core-verification-k3d.py` for the local runtime
suite. See `lab/README.md` for required images and cluster settings.
