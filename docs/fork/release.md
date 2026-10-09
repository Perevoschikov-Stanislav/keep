# Release and deployment preparation

Build from the upstream revision in `FORK_UPSTREAM` plus the reviewed fork
commit. Preserve the upstream license and author history. Local configuration,
database dumps, bridge state and verification output belong in ignored
`.lab-work/`; they are not release inputs in Git.

## Images and GitLab CI

`.gitlab-ci.yml` checks the wire contracts and changed fork frontend suites.
The frontend selector requires complete Git history (`GIT_DEPTH=0`). The
incident contract job uses minimal dependencies; two application compatibility
checks are skipped there. The full PostgreSQL/k3d check below runs those checks
with application dependencies and rejects skipped tests.

The manual `images` job builds and publishes:

| Component | Immutable reference |
| --- | --- |
| Backend | `$CI_REGISTRY_IMAGE/keep-api:$CI_COMMIT_SHA` |
| Frontend | `$CI_REGISTRY_IMAGE/keep-ui:$CI_COMMIT_SHA` |
| Optional MM adapter | `$CI_REGISTRY_IMAGE/keep-mm-bridge:$CI_COMMIT_SHA` |

It uses GitLab registry credentials through stdin and produces `images.env`.
There is no deployment job. Configure a Docker executor runner with privileged
Docker-in-Docker support and a shared `/certs/client` volume for TLS. Registry
namespace, Kubernetes pull Secret and target cluster are installation inputs.

For local builds, set temporary directories inside `.lab-work` and use the
same Dockerfiles:

```bash
mkdir -p .lab-work/build/tmp
export TMPDIR="$PWD/.lab-work/build/tmp" TMP="$PWD/.lab-work/build/tmp" TEMP="$PWD/.lab-work/build/tmp"
release_tag=$(git rev-parse HEAD)
docker build -f docker/Dockerfile.api -t "keep-backend:$release_tag" .
docker build -f docker/Dockerfile.ui --build-arg "GIT_COMMIT_HASH=$release_tag" --build-arg "KEEP_VERSION=$release_tag" -t "keep-ui:$release_tag" keep-ui
docker build -f transports/mattermost/Dockerfile -t "keep-mm-bridge:$release_tag" transports/mattermost
```

The backend excludes `ee/` and defaults to OSS mode with PostHog and Sentry
disabled. Keep the explicit deployment settings as well. The UI disables Next
telemetry; OSS feature visibility and API guards apply at runtime.

## Verification before publication

1. Run `node keep-ui/scripts/test-fork.cjs` from the repository root.
2. Run the full backend check against the selected release image:

   ```bash
   python3 -B lab/check-incident-core-verification-k3d.py --base-image "keep-backend:$release_tag" --image "keep-release-check:$release_tag" --bridge-mattermost
   ```

   This uses private PostgreSQL and requires an initialized local MM service
   for transport checks. It does not use the running lab database.
3. Rehearse schema upgrade, team backfill, silence migration and explicit
   resource adoption on an isolated database copy. Compare historical IDs,
   fingerprints, raw events, incident manual fields and links before/after.
   Repeat backfill/apply and restore the original dump into a separate database
   to verify the backup. A current lab dump already at the Alembic head checks
   repeatability, not the initial schema upgrade. Verify the latter on a copy
   at the upstream schema; label reconstructed fixtures explicitly.
4. Validate/render the selected deployment draft with
   `lab/check-deploy-example.py --source-dir /path/to/keep-chart
   --dependencies /path/to/pinned-helm-archives --example-dir
   .lab-work/review/deploy-draft`. The source checkout is read-only.
5. Deploy the images in the explicitly local k3d lab. Run live correlation,
   SSO/role/team, silence and transport-restart checks from `lab/README.md`.
   Confirm one sender per scope, unchanged post IDs after restart and no
   hydration errors. Retain private results and image IDs outside Git.

## Target cutover

Start from `config/incident-deploy.example/README.md`. Prepare real policies and
destinations in a private review directory using the read-only source IaC.
Replace example image references with the three values in `images.env`, and
set the existing Kubernetes pull Secret name. Review full-path Keycloak role
and team groups independently; team names and visibility come from IaC.

Immediately before deployment, obtain a fresh read-only resource inventory and
preview against the target database. Adoption IDs/digests from the lab are not
production IDs. Review actual UTC cutover time and any active IDs selected for
recreation, then update route CEL fences and artifact hashes. `cutover.yaml`
documents the selection; only the bundle affects runtime.

Stop the legacy sender and its notification workflows before activating the
new owner. Keep `PROVISION_RESOURCES=false` until explicit apply with all three
digests from the fresh preview. Check new events, ACK/assign/resolve, silence
create/edit/cancel/expiry and recovery on the current incident state. Historical
events must not create a notification storm during cutover.

`gitops.enabled=false` also disables the wrapper's legacy `keep-ghosts` CronJob.
Configure the native Alertmanager reconciler through reviewed source URLs and
raw-label team boundaries before enabling it. The generic deployment template
keeps it disabled until those inputs are supplied; do not run both controllers
for the same scope.

Application rollback retains additive tables, silence audit, delivery history
and command receipts. Do not downgrade populated schema or restore an old dump
over newer events. A failed cutover needs a reviewed sender/ownership rollback
and reconciliation of effects already delivered.

GitLab runner and registry references:

- https://docs.gitlab.com/ci/docker/docker_in_docker/
- https://docs.gitlab.com/ci/docker/authenticate_registry/
- https://docs.gitlab.com/user/packages/container_registry/build_and_push_images/
