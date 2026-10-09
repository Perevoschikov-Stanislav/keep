# Teams, silences and incident policies

Configuration examples contain synthetic data. Actual user memberships, URLs,
channel IDs, credentials and adoption targets belong to deployment IaC.

## Teams and roles

Keep reads `KEEP_TEAMS_CONFIG_FILE` or `KEEP_TEAMS_CONFIG`; the file takes
precedence. An active IncidentPolicies snapshot owns access through its `access`
artifact and takes precedence over either environment form.

A role group grants `admin`, `responder` or `viewer`. A separate team group
grants membership. Unknown groups grant no role. With `visibility: team`, a
user without team membership sees no team data. Admin sees all data.

`visible_to` lists teams permitted to read another team's events, defaulting to
its own ID. Users in multiple teams see their combined visibility. Reading
access never grants editing rights: responders modify only their member teams,
and viewers cannot mutate events. `visibility: all` changes reading access,
including unassigned data, without widening responder write access.

Settings → Users and Access → Teams displays the active policy via the read-only
`GET /auth/teams` endpoint. IaC owns edits. `incident_views` configures sidebar
IDs, names and CEL; `ALL` is added automatically. Views are filters and do not
grant access.

Mappings assign a zone, and the policy assigns each zone to one team. Unknown
zones remain unassigned. Include a team discriminator in alert fingerprints.
A fingerprint with history across different teams is hidden from team users in
isolated mode; responders cannot modify foreign, mixed or unassigned history.
An admin must resolve those collisions. Incidents cannot link foreign alerts.

Before enabling isolation on an existing database, preview historical ownership
with `python -m keep.identitymanager.team_backfill`; `--apply` persists it.
Only a zone saved in the event establishes its historical owner. Existing
non-null owners are retained; mixed/unassigned incidents remain unassigned.
See `docs/fork/silences/migration.md` for the transition procedure.

The reverse proxy must strip client supplied identity headers and supply trusted
OAuth2 Proxy headers. Admin API keys remain unrestricted; do not issue them to
team users. Role mapping and membership are independent of notification channels.

## IncidentPolicies

`KEEP_INCIDENT_POLICIES_CONFIG_FILE` loads the versioned bundle. It publishes
TeamPolicy, managed mappings/extraction/workflows, normalization, correlation,
lifecycle, automation and notification settings atomically. Invalid input keeps
the last active version, including after restart.

Administration API: `/settings/incident-policies`. Apply requires admin settings
permissions and the exact active/candidate/preview digests from a fresh preview
on the target database. Ordinary CRUD cannot overwrite managed resources.
Renaming preserves IDs; adoption, deletion and restore require explicit plans.
Operator state, manual notes and history are retained.

Artifact hashes cover exact file bytes. Mount files without changing whitespace;
ConfigMap `|` preserves the trailing newline required by the shipped hashes.
`PROVISION_RESOURCES=false` disables startup provisioning; an explicit policy
apply and the saved active snapshot still work.

Documentation: `docs/fork/incident-core/`. The bundle configures grouping keys,
windows, presentation fields/links/colors, reopen/flapping, ACK deadlines,
escalation, reminders, contacts and ticket workflows. Names and timers are data.

| Example | Feature |
| --- | --- |
| `team-policy.example.yaml` | Roles, membership and reading rules |
| `incident-policies.example/a,b` | Atomic provisioning and managed resources |
| `event-normalization.example/a,b` | Derived fields, collections and presentations |
| `incident-correlation.example/a,b` | Team scoped grouping and required fields |
| `incident-lifecycle.example/a,b` | Episodes, reopen and flapping |
| `incident-automation.example/a,b` | Deadlines, escalation and workflow actions |
| `incident-notifications.example/a,b` | HTTP JSON and Mattermost delivery profiles |
| `incident-bridge.example/a,b` | Optional transport, receipts and silence events |
| `incident-migration.example/a,b` | Explicit legacy ownership/migration plans |
| `incident-legacy.example` | Synthetic legacy compatibility configuration |
| `incident-core.lab` | Synthetic local core configuration |
| `incident-core.target` | Target configuration template |
| `incident-deploy.example` | Parent chart patch, overlays and Keycloak fragments |

## Silences and notifications

`/silences` v1 stores persistent rules, command receipts, coverage and audit.
Responders manage member-team rules; viewers read. `KEEP_READ_ONLY=true` blocks
writes. A silence does not change the canonical alert status or discard events.

Notification actions declare `notification: true` and check fresh canonical
state, team scope and coverage before each attempt and retry. Other actions
continue. Partial aggregates require a safe payload projection; otherwise the
whole notification is skipped with a diagnostic reason.

Routes select destinations and ready presentations. Adapter capabilities, URLs,
credentials, timeout/retry/rate/debounce and callbacks are IaC. A route may use
`after_silence: current_active` to send one current active projection after the
last blocker ends, without replaying suppressed history or restarting SLA.
`initial_delivery: active_only` prevents the first post for a closed incident;
confirmed bindings still receive resolved updates.

Registered integrations use permission ceilings and verified operator proof.
A service identity or message channel does not grant a human role. Confirmation
links open Keep and enforce the actual user's scopes and teams. Unknown external
create results remain held until verified recovery; other destinations continue.

Alertmanager export/import uses configured raw-label team boundaries and explicit
ownership. Externally managed rules remain read-only in Keep. See
`docs/fork/silences/alertmanager.md`. Legacy Maintenance remains enabled by default;
set `KEEP_MAINTENANCE_DESTRUCTIVE_DROP=false` only after reviewed migration.
