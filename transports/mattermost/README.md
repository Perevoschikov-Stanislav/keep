# Mattermost transport v1

Canonical source is this directory. An optional local chart copy lives in the
sibling `../mm-bridge` directory.

Keep owns grouping, titles, lifecycle, routing, timers, escalation and silences.
The adapter renders a ready Notification v1, stores transport bindings and receipts
in SQLite, and writes Mattermost posts. It never reads raw alerts, writes incident
summaries, assigns a role to a user, or maintains a separate snooze.

## IaC

Build the image with `docker build -t keep-mm-bridge:transport-v1 transports/mattermost`.
Deploy using the standalone `chart/`; configure URLs, destination/channel IDs,
credential references, persistence, timeout and recovery bounds in Helm values.
`config.example.json` shows the bridge configuration. One replica with persistent
SQLite and Recreate is required. The database uses new `delivery`, `binding` and
`silence_post` tables;
legacy snooze is not imported implicitly. Snapshot the previous database before
switching. Temporary files stay in `/state/tmp` (or the test Job's `/work`).

The matching IncidentPolicies bundle needs:

* A Mattermost transport with `adapter_ref: mattermost-bridge-v1`, capabilities
  `{update: true, actions: false, receipts: true}`, the bridge URL and bearer
  `auth_ref`. `callback_client_ref` identifies its registered service client.
* Destinations with the same IDs, team IDs and channel IDs as the bridge configuration.
* A service client with separate credential, the destination team ceiling and
  `read:incident`, `read:silence`, `update:notification` scopes. This credential is
  not an ordinary admin API key. `proof_profile_refs: []` is valid for this
  read/receipt-only client; delegated mutation scopes require a proof profile.
* An independent `http-json-v1` lifecycle transport using the bridge URL and bearer
  credential, with `/events` destinations and the required silence subscribers.
* Presentation actions including `silence` and `ack` with
  `actions_fallback: keep_link`. Links open confirmation in Keep. A Mattermost
  message/callback/channel does not prove an operator identity; the bridge does
  not create a silence using its service credential.

Keep already supports direct Mattermost and generic HTTP JSON profiles. Installing
this adapter does not require selecting Mattermost for other destinations.

## Protocol and failure behavior

`POST /notify` receives `{schema_version, notification, channel_id, contacts,
delivery_mode, external_id}`; the Notification is the existing common v1 DTO.
The adapter checks tenant/team/destination and requests the current queue projection
from `GET /integrations/notifications/deliveries/{notification_id}`. A changed
projection, route, lease, ownership or silence prevents the external effect.
Authentication is a bearer transport token. Legacy `{incident_id,event}` is rejected.
The Keep sender timeout must cover the gate read, optional existing-post read and
the Mattermost write, with a margin over their individual bridge timeouts. Silence
annotation repair runs separately through lifecycle events and reconciliation;
ordinary POST/PUT receipts do not wait for a scan of other incident cards.

The intent is committed before POST/PUT. Confirmed duplicates return the same post
ID. A lost response/crash holds the delivery; the adapter searches bounded channel
pages for the exact notification/incident/projection markers and sends a registered
receipt. Keep verifies it using `GET /api/v4/posts/{id}` on the adapter. No evidence
means no blind repeat. Duplicate marker matches also stay held. Recovery cannot
promise exactly once under arbitrary external deletion or tampering.

`POST /events` accepts Silence LifecycleEvent v1. It reads current effective
coverage from Keep and updates service annotations on existing posts. Actor,
comment, expiry, source and affected silence IDs are projected; ordinary alert
delivery remains suppressed. Repeated/stale events cannot restore cancelled rules.
Periodic reconciliation repairs annotations after missed events or restart and
does not generate ordinary posts, reminders or escalations. With no object post,
the default is to create no alert post; engineers use Keep's Silences registry.
Set `destinations.<id>.silence_service_posts: true` to also create/update a dedicated
silence rule card, including when the suppressed incident has no post yet. This
card contains only canonical rule state, actor, comment, expiry and source. It has
its own durable intent and marker recovery, including a content signature: expiry
can change the displayed state without changing a rule revision. An unconfirmed
service intent stays held without blocking other cards or confirmed receipts.
It never creates an alert notification.

The Silence action opens the canonical incident editor in Keep; Manage silences
opens the canonical registry for cancelling a specific rule. Overlapping rules
remain independent. Clicking a link alone does not mutate state.

## Transition

Prepare the reviewed migration export/preview and preserve manual state. Pause legacy
notifications, polling and escalation before enabling the core sender for a scope.
New posts bind one canonical incident/destination. Existing confirmed new-style
posts can be adopted from Keep's external binding after marker verification.
Unmarked/mixed legacy posts require an explicit reviewed transition; the bridge
refuses automatic adoption. Old admin callbacks and manual legacy grouping do not
become a new command API.

The source/configuration sample and tests are not a production cutover plan.
Tests run in k3d on their own PostgreSQL and private Mattermost channels.

## Tests

```bash
docker build -f lab/Dockerfile.core-compat-check -t keep-bridge21-check:local .
python3 -B lab/check-mm-bridge-k3d.py
```

The runner takes the bot token from the existing local Kubernetes Secret, never
prints its value, and refuses any non-lab Mattermost endpoint. Per-test channels
are private and archived during cleanup. Main Keep data and old bridge are untouched.

Refresh the local chart/source copy using
`python3 -B transports/mattermost/sync-chart-copy.py`. The canonical implementation
remains here under Git. Contracts and integration documentation: `docs/fork/silences/`.
