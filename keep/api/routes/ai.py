import logging
import os

from fastapi import APIRouter, Depends, HTTPException

from keep.api.core.db import (
    get_alerts_count,
    get_first_alert_datetime,
    get_incidents_count,
    get_or_create_external_ai_settings,
    update_extrnal_ai_settings,
)
from keep.api.models.ai_external import ExternalAIConfigAndMetadataDto
from keep.identitymanager.authenticatedentity import AuthenticatedEntity
from keep.identitymanager.identitymanagerfactory import IdentityManagerFactory

router = APIRouter()
logger = logging.getLogger(__name__)
OSS_ONLY = os.getenv("KEEP_OSS_ONLY", "true").lower() == "true"


@router.get(
    "/stats",
    description="Get stats for the AI Landing Page",
    include_in_schema=False,
)
def get_stats(
    authenticated_entity: AuthenticatedEntity = Depends(
        IdentityManagerFactory.get_auth_verifier(["read:alert"])
    ),
):
    if OSS_ONLY:
        raise HTTPException(status_code=404, detail="Not found")
    tenant_id = authenticated_entity.tenant_id
    external_ai_settings = get_or_create_external_ai_settings(tenant_id)

    for setting in external_ai_settings:
        setting.algorithm.remind_about_the_client(tenant_id)

    return {
        "alerts_count": get_alerts_count(tenant_id),
        "first_alert_datetime": get_first_alert_datetime(tenant_id),
        "incidents_count": get_incidents_count(tenant_id),
        "algorithm_configs": external_ai_settings,
    }


@router.put(
    "/{algorithm_id}/settings",
    description="Update settings for an external AI",
    include_in_schema=False,
)
def update_settings(
    algorithm_id: str,
    body: ExternalAIConfigAndMetadataDto,
    authenticated_entity: AuthenticatedEntity = Depends(
        IdentityManagerFactory.get_auth_verifier(["write:alert"])
    ),
):
    if OSS_ONLY:
        raise HTTPException(status_code=404, detail="Not found")
    tenant_id = authenticated_entity.tenant_id
    return update_extrnal_ai_settings(tenant_id, body)
