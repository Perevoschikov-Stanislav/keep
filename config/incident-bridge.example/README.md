# Ready bridge examples

Both bundles use arbitrary teams `frontend-west` and `network-east`. All grouping,
normalization, lifecycle, SLA, contact addresses, route priorities and presentation
actions remain in IncidentPolicies. `primary` is the ready Mattermost bridge;
`secondary` remains generic HTTP JSON. The bridge is not a required transport for
Keep. Files are offline examples; addresses and channel IDs must be configured.

`a/bridge.json` and `b/bridge.json` match the destination IDs/teams/channels in their
bundles. Recovery intervals/page sizes, sender timeout and visible actions differ.
Bridge HTTP timeouts are 2s/3s; sender timeouts are 8s/12s, covering a gate read,
optional existing-post read and the post write. Configure the sender budget for
all sequential transport calls, with a margin; annotation repair is independent.
The read/receipt service client has no delegated mutation scope or OIDC proof
profile. Human commands use the authenticated Keep UI confirmation. Adding a
delegated mutation scope requires a configured proof profile again.

Secrets are references only. Mount both the pinned access policy and bundle using
the normal Keep provisioning procedure. Deploy the adapter with the matching JSON
configuration. For a new configuration, review the preview and keep only one active
sender per scope. These examples do not configure the production Enterprise transition.

The separate `silence-events` HTTP JSON transport/subscriber targets `/events`.
By default it updates existing object cards only. Set
`bridge.json: destinations.<id>.silence_service_posts: true` to also publish a
dedicated canonical silence rule card without needing an incident post.
Variant `b` enables that option; variant `a` keeps it disabled.

Validation:

```bash
python3 -B lab/incident_contract.py validate config/incident-bridge.example/a/bundle.yaml --tenant keep
python3 -B lab/incident_contract.py validate config/incident-bridge.example/b/bundle.yaml --tenant keep
```
