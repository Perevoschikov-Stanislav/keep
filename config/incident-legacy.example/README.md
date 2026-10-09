# Legacy compatibility example

This example uses synthetic deployment data and explicit legacy runtime
ownership. It demonstrates a separate role mapping and team membership,
existing mapping/extraction/correlation resources and notification workflows.
It does not export production users, channels or incident ownership.

`source-manifest.json` pins content hashes for the external compatibility input;
it contains no local checkout path or user identity. To check a different bridge,
review and replace that manifest and the matching fixtures. Input data must
match its source snapshot; merely passing schema validation is not a migration
or compatibility proof.

Run the isolated check through `lab/run-core-compatibility.py --source-root
/path/to/iac-checkout --example-dir /path/to/matching-fixtures`. The chart/bridge
snapshot and fixtures are read-only. The runner defaults to the local fixtures
in `.lab-work/config/incident-legacy.example` when available. Synthetic public
examples do not match an unchanged external deployment byte for byte. For native core
ownership use a reviewed target policy and migration plan. Documentation:
`docs/fork/incident-core/` and `docs/fork/silences/migration.md`.
