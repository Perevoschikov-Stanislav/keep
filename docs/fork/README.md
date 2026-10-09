# Fork documentation

The fork provides OSS mode, configurable team isolation and roles, persistent
silences, an incident policy runtime and transport adapters.

- `config/README.md`: teams, roles, policy examples and configuration entry points.
- `incident-core/`: provisioning, normalization, grouping, lifecycle, automation,
  notifications, JSON Schema and protocol fixtures.
- `silences/`: registry contract, integration API, Alertmanager synchronization
  and migration from legacy Dismiss/Maintenance.
- `transports/mattermost/README.md`: the optional Mattermost notification adapter.
- `lab/README.md`: local k3d checks and persistent test artifacts.
- `release.md`: builds, GitLab CI, verification and reviewed deployment cutover.

Deployment examples use synthetic identifiers and example domains. Supply
actual URLs, memberships, resource IDs and credential references through IaC.
