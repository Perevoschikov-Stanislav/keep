"""Normal authenticated API. Delegated integration commands have a separate task."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from keep.api.bl.silences_bl import SilencesBL, fail
from keep.api.bl.silences_evaluator import SilenceEvaluator, read_snapshot
from keep.api.core.config import config
from keep.api.core.silence_integrations import IntegrationConfigurationError
from keep.api.core.db import get_session
from keep.api.models.silence import (
    CancelSilenceCommand, CreateSilenceCommand, EffectiveSilenceQuery,
    EffectiveSilenceResponse, SilenceDto, SilenceListResponse,
    SilenceMutationResponse, State, UpdateSilenceCommand,
)
from keep.identitymanager.authenticatedentity import AuthenticatedEntity
from keep.identitymanager.identitymanagerfactory import IdentityManagerFactory


class SilenceRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def versioned_handler(request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                location = exc.errors()[0].get("loc", ())
                field = ".".join(str(part) for part in location if part not in {"body", "query", "path"}) or None
                status, detail = 422, {"code": "validation_error", "message": "Invalid request",
                                      "field": field, "current_revision": None}
            except HTTPException as exc:
                status = exc.status_code
                detail = exc.detail if isinstance(exc.detail, dict) else {
                    "code": {401: "unauthenticated", 403: "forbidden", 404: "not_found"}.get(status, "validation_error"),
                    "message": {401: "Authentication required", 403: "Insufficient permissions", 404: "Not found"}.get(status, "Invalid request"),
                    "field": None, "current_revision": None,
                }
            except SQLAlchemyError:
                status, detail = 503, {"code": "silence_verification_unavailable", "message": "Silence storage unavailable",
                                      "field": None, "current_revision": None}
            except IntegrationConfigurationError:
                status, detail = 503, {"code": "integration_configuration_unavailable",
                                      "message": "Integration configuration unavailable",
                                      "field": None, "current_revision": None}
            return JSONResponse(status_code=status, content={"schema_version": 1, "detail": detail})

        return versioned_handler


def reject_impersonation(request: Request):
    names = {"X-KEEP-USER", "X-KEEP-ROLE",
             config("KEEP_IMPERSONATION_USER_HEADER", default="X-KEEP-USER"),
             config("KEEP_IMPERSONATION_ROLE_HEADER", default="X-KEEP-ROLE")}
    if any(name in request.headers for name in names):
        fail(403, "forbidden", "Legacy impersonation is not accepted")


router = APIRouter(route_class=SilenceRoute, dependencies=[Depends(reject_impersonation)])
read_identity = IdentityManagerFactory.get_auth_verifier(["read:silence"])
create_identity = IdentityManagerFactory.get_auth_verifier(["write:silence"])
update_identity = IdentityManagerFactory.get_auth_verifier(["update:silence"])


@router.get("", response_model=SilenceListResponse)
def list_silences(
    request: Request,
    state: State | None = None, team_id: str | None = None,
    limit: int = Query(50, ge=1, le=200), cursor: str | None = None,
    entity: AuthenticatedEntity = Depends(read_identity), session: Session = Depends(get_session),
):
    if set(request.query_params) - {"state", "team_id", "limit", "cursor"}:
        fail(422, "validation_error", "Unknown query parameter")
    read_snapshot(session)
    return SilencesBL(session, entity).list(state=state, team_id=team_id or None,
        filter_team="team_id" in request.query_params, limit=limit, cursor=cursor)


@router.post("", response_model=SilenceMutationResponse, status_code=201)
def create_silence(
    command: CreateSilenceCommand, response: Response,
    entity: AuthenticatedEntity = Depends(create_identity), session: Session = Depends(get_session),
):
    result, response.status_code = SilencesBL(session, entity).create(command)
    response.headers["Location"] = f"/silences/{result.result.id}"
    return result


@router.post("/effective", response_model=EffectiveSilenceResponse)
def effective_silences(
    query: EffectiveSilenceQuery, entity: AuthenticatedEntity = Depends(read_identity),
    session: Session = Depends(get_session),
):
    return SilenceEvaluator(session, entity.tenant_id).effective(entity, query.targets)


@router.get("/{silence_id}", response_model=SilenceDto)
def get_silence(
    silence_id: UUID, entity: AuthenticatedEntity = Depends(read_identity),
    session: Session = Depends(get_session),
):
    read_snapshot(session)
    return SilencesBL(session, entity).get(silence_id)


@router.patch("/{silence_id}", response_model=SilenceMutationResponse)
def update_silence(
    silence_id: UUID, command: UpdateSilenceCommand,
    entity: AuthenticatedEntity = Depends(update_identity), session: Session = Depends(get_session),
):
    return SilencesBL(session, entity).update(silence_id, command)[0]


@router.post("/{silence_id}/cancel", response_model=SilenceMutationResponse)
def cancel_silence(
    silence_id: UUID, command: CancelSilenceCommand,
    entity: AuthenticatedEntity = Depends(update_identity), session: Session = Depends(get_session),
):
    return SilencesBL(session, entity).cancel(silence_id, command)[0]
