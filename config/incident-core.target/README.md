# Target incident policy template

All URLs, cluster aliases, channel IDs and resource metadata are synthetic.
Configure actual values through deployment IaC; supply credentials only through
Secret references. Group and role membership are independent. Teams, zones,
views, grouping rules, presentations and transports are configurable.

`cutover.yaml` records the example time and selected active incident IDs. Runtime
routing uses the conditions in `bundle.yaml`; this metadata file is not a loader.
Set the actual UTC transition time and explicitly reviewed active UUIDs in both.
The selected-ID list is empty. Closed history without a confirmed binding is
blocked by `initial_delivery: active_only`.

Mappings, extraction and legacy workflow resources require explicit `adoptions`
when already present in the target database. The template contains none. Validate,
preview and compare resource owners before apply. Old transport SQLite and post
bindings need not be imported when creating fresh messages; Keep history and
manual fields must remain intact. Use one sender per scope.

`lab/prepare-incident-core-target.py --cutover ... --active-ids ...` prepares a
local draft from the source chart selected by `KEEP_IAC_DIR`. Source is read-only;
outputs stay inside the fork. See `config/incident-deploy.example/README.md` for
images, mounts, Keycloak groups, Secret references and Helm verification.
