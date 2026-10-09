"""Admin-only configuration API; artifacts are supplied bytes, not server paths."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session

from keep.api.bl.incident_provisioning import Candidate, IncidentProvisioning, MODELS, current_values, digest, target_row
from keep.api.core import db
from keep.api.core.incident_contract import ContractError
from keep.identitymanager.authenticatedentity import AuthenticatedEntity
from keep.identitymanager.identitymanagerfactory import IdentityManagerFactory

router = APIRouter()


def administrator(entity: AuthenticatedEntity = Depends(IdentityManagerFactory.get_auth_verifier(["read:settings"]))):
    if entity.role != "admin":
        raise HTTPException(403, detail="Configuration administration requires admin")
    return entity


def configuration_writer(entity: AuthenticatedEntity = Depends(IdentityManagerFactory.get_auth_verifier(["write:settings"]))):
    return administrator(entity)


class CandidateRequest(BaseModel):
    bundle: dict
    artifacts: dict[str, str]

    class Config:
        extra = "forbid"


class RestorePreviewRequest(BaseModel):
    generation: int = Field(..., gt=0)
    deletions: list[dict] | None = None

    class Config:
        extra = "forbid"


class RestoreRequest(RestorePreviewRequest):
    expected_active_digest: str | None = Field(...)
    expected_candidate_digest: str = Field(..., regex="^[a-f0-9]{64}$")
    expected_preview_digest: str = Field(..., regex="^[a-f0-9]{64}$")

    class Config:
        extra = "forbid"


class ApplyRequest(CandidateRequest):
    expected_active_digest: str | None = Field(...)
    expected_candidate_digest: str = Field(..., regex="^[a-f0-9]{64}$")
    expected_preview_digest: str = Field(..., regex="^[a-f0-9]{64}$")


def candidate(request, tenant_id):
    return Candidate.load(request.bundle, Path("."), tenant_id, artifact_contents=request.artifacts)


@router.get("")
def status(entity: AuthenticatedEntity = Depends(administrator)):
    return IncidentProvisioning(entity.tenant_id).status()


@router.post("/validate")
def validate(request: CandidateRequest, entity: AuthenticatedEntity = Depends(administrator)):
    try:
        checked = candidate(request, entity.tenant_id)
        return {"candidate_digest": checked.digest, "revision": checked.bundle["revision"], "valid": True}
    except ContractError as error:
        raise HTTPException(400, detail=str(error)) from None


@router.post("/preview")
def preview(request: CandidateRequest, entity: AuthenticatedEntity = Depends(administrator)):
    try:
        return IncidentProvisioning(entity.tenant_id).preview(candidate(request, entity.tenant_id))
    except ContractError as error:
        raise HTTPException(409, detail=str(error)) from None


@router.post("/apply")
def apply(request: ApplyRequest, entity: AuthenticatedEntity = Depends(configuration_writer)):
    try:
        return IncidentProvisioning(entity.tenant_id).apply(candidate(request, entity.tenant_id),
            expected_active_digest=request.expected_active_digest, expected_candidate_digest=request.expected_candidate_digest,
            expected_preview_digest=request.expected_preview_digest, actor=entity.email)
    except ContractError as error:
        raise HTTPException(409, detail=str(error)) from None


@router.get("/resources/{kind}/{target_id}")
def adoption_target(kind: str, target_id: str, entity: AuthenticatedEntity = Depends(administrator)):
    if kind not in MODELS:
        raise HTTPException(404, detail="Resource kind not found")
    try:
        with Session(db.engine) as session:
            row = target_row(session, entity.tenant_id, kind, target_id)
            if row is None:
                raise HTTPException(404, detail="Resource not found")
            return {"kind": kind, "target_id": str(row.id), "resource_digest": digest(current_values(kind, row))}
    except ContractError as error:
        raise HTTPException(400, detail=str(error)) from None


@router.get("/versions/{generation}/preview")
def restore_preview(generation: int, entity: AuthenticatedEntity = Depends(administrator)):
    try:
        service = IncidentProvisioning(entity.tenant_id)
        return service.preview(service.restore_candidate(generation))
    except ContractError as error:
        raise HTTPException(409, detail=str(error)) from None


@router.post("/restore")
def restore(request: RestoreRequest, entity: AuthenticatedEntity = Depends(configuration_writer)):
    try:
        service = IncidentProvisioning(entity.tenant_id)
        return service.apply(service.restore_candidate(request.generation, deletions=request.deletions),
            expected_active_digest=request.expected_active_digest, expected_candidate_digest=request.expected_candidate_digest,
            expected_preview_digest=request.expected_preview_digest, actor=entity.email)
    except ContractError as error:
        raise HTTPException(409, detail=str(error)) from None


@router.post("/restore/preview")
def restore_preview_with_deletions(request: RestorePreviewRequest, entity: AuthenticatedEntity = Depends(administrator)):
    try:
        service = IncidentProvisioning(entity.tenant_id)
        return service.preview(service.restore_candidate(request.generation, deletions=request.deletions))
    except ContractError as error:
        raise HTTPException(409, detail=str(error)) from None
