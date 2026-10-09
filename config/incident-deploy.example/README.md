# Helm deployment example

This directory contains a parent chart patch, fork image overlays, policy mounts,
Keycloak group/claim fragments and an optional notification transport. Values
use example domains, synthetic channel IDs and image/registry placeholders.
Original local deployment data does not belong in versioned examples.

## Files

| File | Purpose |
| --- | --- |
| `keep-wrapper.patch` | Role allowlist, trusted group claim, impersonation toggle, transport credentials and legacy bridge replicas |
| `values.overlay.yaml` | Images, registry Secret reference, roles, mounted policy and sender/provisioner flags |
| `chart-files/` | Ten policy files plus ConfigMap and Secretor templates |
| `bridge-values.yaml` | Optional MM transport, destinations, Secret references and persistence |
| `transport-chart.patch` | Image pull Secret support in the standalone adapter chart |
| `keycloak/*.json` | Group and client mapper fragments for existing Keycloak IaC |
| `cutover.yaml`, `source-routes.json` | Synthetic transition and route metadata |
| `source.json` | Exact upstream Helm dependency versions |

Role groups grant admin/responder/viewer; `/keep-teams/...` groups grant
membership. A user needs both a role and permitted team membership, except
admin. Add the full-path `keep_groups` mapper to the Keep OIDC client, and
configure OAuth2 Proxy to read it. Review actual users before changing allowlist.
Do not replace an entire realm with these fragments.

## Prepare a review checkout

```bash
example_dir="$PWD/config/incident-deploy.example"
chart_dir=/path/to/review-checkout/keep
patch --dry-run --batch -p1 -d "$chart_dir" -i "$example_dir/keep-wrapper.patch"
patch --batch -p1 -d "$chart_dir" -i "$example_dir/keep-wrapper.patch"
cp -a "$example_dir/chart-files/." "$chart_dir/"
cp "$example_dir/values.overlay.yaml" "$chart_dir/values.fork.yaml"
```

Supply Helm values in order `values.yaml`, `values.fork.yaml`, or merge the
overlay with reviewed base values. Lists replace entirely; do not shorten the
existing backend `env` list and lose database/auth settings. Explicit `env`
entries take precedence over `envRender`. Review patch applicability against
the selected source chart; it does not contain source hostnames.

Copy `transports/mattermost/chart/` to a separate review checkout, apply
`transport-chart.patch` there and use `bridge-values.yaml` as its Helm values.
The example release is `keep-notification-bridge` in `monitoring`; if renamed,
update endpoints in the bundle and bridge configuration. Register it through
the installation's normal GitOps process.

Replace `REPLACE_WITH_PUBLISHED_TAG`, `REPLACE_WITH_REGISTRY_SECRET`, repository
names, example URLs and every synthetic channel ID. Publish images before using
their tags. Keep the exact upstream dependency pins until replacements exist.

## Credentials and configuration

| Secret reference | Consumers |
| --- | --- |
| `keep-core-bridge/transport` | Keep sender and bridge bearer authentication |
| `keep-core-bridge/service` | Read/receipt-only registered recovery client |
| `keep-mm-bot/MM_BOT_TOKEN` | The transport only |

New credential fields use the existing Secretor operator. Check its availability
and the bot's actual channel permissions. DB/Redis/JWT/NextAuth references stay
in base values. Removing the old bridge from `KEEP_DEFAULT_API_KEYS` does not
revoke an already saved key; revoke it separately after stopping that sender.

ConfigMap `|` preserves exact trailing newlines. Update artifact SHA-256 whenever
files change, and change the policy revision pod annotation to trigger reload.
The mount uses no `subPath`; policy activation still needs startup provisioning
or explicit apply. `PROVISION_RESOURCES=false` intentionally requires the first
reviewed apply and preserves use of the active database snapshot.

## Transition

1. Check schema migrations, legacy silence migration and historical team owners
   on a database copy. Review mixed fingerprints and unassigned history.
2. Validate actual Keycloak memberships, URLs, registry credentials and destinations.
3. Obtain existing resource IDs/digests and fill explicit `adoptions` from the
   read-only `/settings/incident-policies/resources/{kind}/{target_id}` endpoint.
4. Select the actual UTC cutover time and active IDs. Update route CEL fences and
   `cutover.yaml`; the metadata file itself does not affect runtime.
5. Validate and preview the bundle against the target database. Stop legacy
   sender, old PostSync provisioner and all duplicate notification workflows.
6. Start the transport, then apply using active/candidate/preview digests from
   that fresh preview. Offline example digests are not substitutes.
7. Check role/team isolation, ACK/assign/resolve, silences and expiry, queue recovery,
   closed-history handling and views. Keep one sender per scope.

Disable legacy Maintenance dropping only after reviewed silence migration.
Presets/facets/dashboards remain existing DB resources and are not managed by
IncidentPolicies v1. Documentation: `docs/fork/` and `config/README.md`.

## Local verification

Keep `0.1.97`, Redis `19.5.5` and OAuth2 Proxy `7.8.1` archives are required.
Download/cache them under `.lab-work`, setting `TMPDIR`, `HELM_CACHE_HOME`,
`HELM_CONFIG_HOME` and `HELM_DATA_HOME` to directories there.

```bash
python3 -B lab/check-deploy-example.py --source-dir /path/to/keep-chart --dependencies /path/to/helm-archives
```

The checker makes a redacted local source copy, applies both patches, runs
Helm lint/template, validates ENV/Secret wiring and groups, compares mounted
policy bytes and checks hashes/contracts. It verifies unchanged input hashes
and never deploys. Results stay in ignored `.lab-work`. Source chart credentials,
production connectivity and live role assignments require their own checks.
