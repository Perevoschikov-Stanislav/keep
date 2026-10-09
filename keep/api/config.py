import logging
import os

import keep.api.logging
from keep.api.alert_deduplicator.deduplication_rules_provisioning import (
    provision_deduplication_rules_from_env,
)
from keep.api.api import AUTH_TYPE
from keep.api.bl.mapping_rules_provisioning import provision_mapping_rules_from_env
from keep.api.core.db_on_start import migrate_db, try_create_single_tenant
from keep.api.core.dependencies import SINGLE_TENANT_UUID
from keep.api.core.tenant_configuration import TenantConfiguration
from keep.api.routes.dashboard import provision_dashboards
from keep.identitymanager.identitymanagerfactory import IdentityManagerTypes
from keep.providers.providers_factory import ProvidersFactory
from keep.providers.providers_service import ProvidersService
from keep.workflowmanager.workflowstore import WorkflowStore

PORT = int(os.environ.get("PORT", 8080))
PROVISION_RESOURCES = os.environ.get("PROVISION_RESOURCES", "true") == "true"

keep.api.logging.setup_logging()
logger = logging.getLogger(__name__)


def provision_resources():
    if PROVISION_RESOURCES:
        logger.info("Loading providers into cache")
        # provision providers from env. relevant only on single tenant.
        logger.info("Provisioning providers and workflows")
        ProvidersService.provision_providers(SINGLE_TENANT_UUID)
        logger.info("Providers loaded successfully")
        from keep.api.bl.incident_provisioning import Candidate, IncidentProvisioning
        from keep.api.core import db
        from keep.api.core.incident_contract import ContractError
        from sqlmodel import Session

        service = IncidentProvisioning(SINGLE_TENANT_UUID)
        bundle_path = os.environ.get("KEEP_INCIDENT_POLICIES_CONFIG_FILE")
        managed = service.status()["active_digest"] is not None
        if bundle_path:
            try:
                candidate = Candidate.from_file(bundle_path, SINGLE_TENANT_UUID)
                preview = service.preview(candidate)
                service.apply(candidate, expected_active_digest=preview["active_digest"],
                              expected_candidate_digest=preview["candidate_digest"],
                              expected_preview_digest=preview["preview_digest"], actor="iac-startup")
                managed = True
                logger.info("Incident configuration active: generation %s", service.status()["generation"])
            except ContractError as error:
                logger.error("Incident configuration retained: %s", error)
                # Do not replace the failed bundle with independently applied legacy input.
                managed = True
        if not managed:
            try:
                with Session(db.engine) as session, session.begin():
                    WorkflowStore.provision_workflows(SINGLE_TENANT_UUID, session=session)
                    provision_mapping_rules_from_env(SINGLE_TENANT_UUID, session=session)
                logger.info("Legacy workflows and mappings provisioned successfully")
            except (ValueError, OSError):
                logger.error("Legacy configuration invalid; workflows and mappings retained")
        provision_dashboards(SINGLE_TENANT_UUID)
        logger.info("Dashboards provisioned successfully")
        logger.info("Provisioning deduplication rules")
        provision_deduplication_rules_from_env(SINGLE_TENANT_UUID)
        logger.info("Deduplication rules provisioned successfully")
    else:
        logger.info("Provisioning resources is disabled")


def on_starting(server=None):
    """This function is called by the gunicorn server when it starts"""
    logger.info("Keep server starting")

    migrate_db()
    from keep.api.core.incident_configuration import reset_configuration_cache
    reset_configuration_cache()

    # Load this early and use preloading
    # https://www.joelsleppy.com/blog/gunicorn-application-preloading/
    # @tb: 👏 @Matvey-Kuk
    ProvidersFactory.get_all_providers()
    # Load tenant configuration early
    TenantConfiguration()

    # Create single tenant if it doesn't exist
    if AUTH_TYPE in [
        IdentityManagerTypes.DB.value,
        IdentityManagerTypes.NOAUTH.value,
        IdentityManagerTypes.OAUTH2PROXY.value,
        IdentityManagerTypes.ONELOGIN.value,
        IdentityManagerTypes.KEYCLOAK.value,
        IdentityManagerTypes.OKTA.value,
        "no_auth",  # backwards compatibility
        "single_tenant",  # backwards compatibility
    ]:
        excluded_from_default_user = [
            IdentityManagerTypes.OAUTH2PROXY.value,
            IdentityManagerTypes.ONELOGIN.value,
            IdentityManagerTypes.KEYCLOAK.value,
            IdentityManagerTypes.OKTA.value,
        ]
        # for oauth2proxy, we don't want to create the default user
        try_create_single_tenant(
            SINGLE_TENANT_UUID,
            create_default_user=(
                False if AUTH_TYPE in excluded_from_default_user else True
            ),
        )

    provision_resources()

    if os.environ.get("USE_NGROK", "false") == "true":
        from pyngrok import ngrok
        from pyngrok.conf import PyngrokConfig

        ngrok_config = PyngrokConfig(
            auth_token=os.environ.get("NGROK_AUTH_TOKEN", None)
        )
        # If you want to use a custom domain, set the NGROK_DOMAIN & NGROK_AUTH_TOKEN environment variables
        # read https://ngrok.com/blog-post/free-static-domains-ngrok-users -> https://dashboard.ngrok.com/cloud-edge/domains
        ngrok_connection = ngrok.connect(
            PORT,
            pyngrok_config=ngrok_config,
            domain=os.environ.get("NGROK_DOMAIN", None),
        )
        public_url = ngrok_connection.public_url
        logger.info(f"ngrok tunnel: {public_url}")
        os.environ["KEEP_API_URL"] = public_url

    logger.info("Keep server started")


def post_worker_init(worker):
    # We need to reinitialize logging in each worker because gunicorn forks the worker processes
    print("Init logging in worker")
    logging.getLogger().handlers = []  # noqa
    keep.api.logging.setup_logging()  # noqa
    print("Logging initialized in worker")


post_worker_init = post_worker_init
