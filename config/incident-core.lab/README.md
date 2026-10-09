# Local incident policy template

This directory contains synthetic mappings, extraction, grouping, presentation,
team access and notification routes. It is a template, not an export of an
existing lab. Replace channel IDs and URLs and adopt actual resources through a
reviewed preview before use. Examples do not contain live adoption targets.

Keep owns composition, lifecycle, assignment, silences and notification policy.
The optional bridge delivers ready messages and retains external bindings only.
Source alert objects/descriptions/links are configurable presentation collections.
`after_silence: current_active` sends the latest active state after the last
blocker ends; `initial_delivery: active_only` avoids first posts for closed history.

Validate with `python3 -B lab/incident_contract.py validate
config/incident-core.lab/bundle.yaml --tenant keep`. Apply requires a fresh preview
on the target database and all three expected digests. Use `lab/README.md` for
k3d configuration, synthetic journeys and private local artifact storage.
