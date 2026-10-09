# Local k3d lab

The lab runs in `k3d-local/keep-lab`. Cluster-changing helpers guard a loopback
Kubernetes API; use them only with the intended local cluster. Core, SSO and
notification configuration come from IaC files and runtime Secret references.

## Configuration

- `team-policy.yaml`: role groups, team membership, zones and sidebar views.
- `keycloak-memberships.yaml`: synthetic lab users and groups; credentials remain
  in Keycloak. Synchronize with `sync-keycloak-memberships.py`.
- `keep-backend.patch.json`, `keep-frontend.image.patch.json`, `keep-gateway.json`:
  local deployment patches. Image tags and endpoints are environment specific.
- `alertmanager-reconciliation.json`: raw-label boundaries for the synthetic lab.
- `mattermost-state.json`, `mattermost-state.patch.json`: persistent MM state.
- `config/incident-core.lab/`: synthetic incident policy template.

All output, cache, temporary files and local runtime state stay in `.lab-work/`,
which is ignored by Git and Docker builds. Do not place credentials or real user
exports in versioned fixtures. Local runtime/configuration snapshots are private
inputs; examples use neutral domains and synthetic identifiers.

To translate an external deployment, set `KEEP_IAC_DIR` to its read-only Keep
chart directory. Compatibility checks require an explicit `--source-root` and
matching local fixtures (`--example-dir`); public examples contain synthetic data.
Chromium comes from Playwright; `KEEP_LAB_CHROME` overrides the executable.
Generated real configuration goes to `.lab-work/config`, never the versioned
examples. `KEEP_LOCAL_CONFIG_ROOT` changes that local input/output directory.

## Checks

Install the contract dependencies from `requirements.contract-check.txt` and
application dependencies from the lock file, or use the local verification
images described by the Dockerfiles. Supply available image tags explicitly.

| Command | Coverage |
| --- | --- |
| `python3 -B lab/check-silences-contract.py` | Silence schema and wire fixtures |
| `python3 -B lab/check-incident-contract.py` | Policy schema, references and examples |
| `python3 -B lab/check-team-isolation.py` | Role/team permissions and ownership |
| `python3 -B lab/check-silences-integration-k3d.py` | Integration proof, lifecycle and receivers |
| `python3 -B lab/check-incident-provisioning-k3d.py` | Atomic apply, adoption, drift and restore |
| `python3 -B lab/check-event-normalization-k3d.py` | Normalization and presentation |
| `python3 -B lab/check-incident-correlation-k3d.py` | Grouping and team boundaries |
| `python3 -B lab/check-incident-lifecycle-k3d.py` | Episode transitions and flapping |
| `python3 -B lab/check-incident-automation-k3d.py` | Deadlines, workflows and reminders |
| `python3 -B lab/check-incident-notifications-k3d.py` | Delivery queue, actions and receipts |
| `python3 -B lab/check-incident-migration-k3d.py` | Legacy state inventory and explicit plans |
| `python3 -B lab/check-incident-core-verification-k3d.py` | Combined runtime suite |
| `python3 -B lab/check-mm-bridge-k3d.py` | Optional MM adapter and recovery |
| `node lab/check-ui.cjs` | Actual SSO, roles and team visibility |

The k3d runner accepts `--image`, `--base-image`, `--skip-build` and
`--skip-import`; consult `--help`. It creates an isolated verification Job with
its own data. Browser/live helpers may create and cancel synthetic rules; they
are local checks, not read-only production diagnostics.

For an existing core verification run, `KEEP_CORE_RUN_DIR` selects its local
artifact directory. Live checks, browser checks and transport-restart checks
are `check-incident-core-live.py`, `check-incident-core-browser.cjs` and
`check-incident-core-restart.py`. Inputs must match the active lab configuration;
synthetic versioned examples are not exports of its current channels or owners.
`KEEP_LAB_SCENARIO_DIR` can select saved `live-events.json` and
`live-incidents.json` inside `.lab-work` for replay against an existing lab.
It must contain all sixteen rule families; each replay gets fresh timestamps
and fingerprints. Keep `channels.json` and the active policy generation in the
selected run directory for browser and recovery checks.

## Deployment template

`config/incident-deploy.example/README.md` documents the parent chart patch,
Keycloak fragments and separate notification transport. Run
`check-deploy-example.py --source-dir /path/to/keep-chart --dependencies
/path/to/helm-archives` for local lint/render and exact mounted-file validation.
Use `--example-dir .lab-work/review/deploy-draft` for a prepared private draft.
It makes a redacted copy of the source chart inside `.lab-work` and checks that
the input files are unchanged. It does not deploy either chart.

Core transition requires migrations, historical team backfill, explicit resource
adoptions, a reviewed cutover time/active-ID selection and one notification
sender per owned scope. Legacy sender and notification workflows must stop
before activating the new path. `PROVISION_RESOURCES=false` leaves the initial
bundle inactive until an explicit reviewed apply. Procedures are in
`docs/fork/incident-core/` and `docs/fork/silences/migration.md`.
