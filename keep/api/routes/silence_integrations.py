"""Limited clients read snapshots; mutations also require signed operator proof."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlmodel import Session, select

from keep.api.bl.silences_bl import SilencesBL, fail
from keep.api.bl.silences_evaluator import SilenceEvaluator, read_snapshot
from keep.api.core.db import get_session
from keep.api.core.silence_integrations import get_silence_integrations
from keep.api.models.db.silence import NotificationDelivery
from keep.api.models.silence import (
    CancelSilenceCommand, CreateSilenceCommand, EffectiveSilenceQuery, EffectiveSilenceResponse,
    SilenceDto, SilenceListResponse, SilenceMutationResponse, State, UpdateSilenceCommand,
    utc_now, utc_string,
)
from keep.api.routes.silences import SilenceRoute, reject_impersonation
from keep.identitymanager.authenticatedentity import AuthenticatedEntity
from keep.identitymanager.silence_integration_auth import integration_identity
from keep.identitymanager.team_access import visible_team_ids

router = APIRouter(route_class=SilenceRoute, dependencies=[Depends(reject_impersonation)])
read_identity = integration_identity("read:silence")
create_identity = integration_identity("write:silence", delegated=True)
update_identity = integration_identity("update:silence", delegated=True)


@router.get("", response_model=SilenceListResponse)
def snapshot(request: Request, state: State | None = None, team_id: str | None = None,
    limit: int | None = Query(None, ge=1, le=1000), cursor: str | None = None,
    entity: AuthenticatedEntity = Depends(read_identity), session: Session = Depends(get_session)):
    if set(request.query_params) - {"state", "team_id", "limit", "cursor"}:
        fail(422, "validation_error", "Unknown query parameter")
    size = get_silence_integrations().dispatch["snapshot_page_size"]
    if limit is not None and limit > size:
        fail(422, "validation_error", "Snapshot limit exceeds configured maximum", "limit")
    read_snapshot(session)
    return SilencesBL(session, entity).list(state=state, team_id=team_id or None,
        filter_team="team_id" in request.query_params, limit=limit or size, cursor=cursor)


@router.post("/effective", response_model=EffectiveSilenceResponse)
def effective(query: EffectiveSilenceQuery, entity: AuthenticatedEntity = Depends(read_identity),
    session: Session = Depends(get_session)):
    return SilenceEvaluator(session, entity.tenant_id).effective(entity, query.targets)


@router.get("/deliveries")
def deliveries(request: Request, limit: int = Query(50, ge=1, le=200),
    entity: AuthenticatedEntity = Depends(read_identity), session: Session = Depends(get_session)):
    if set(request.query_params) - {"limit"}:
        fail(422, "validation_error", "Unknown query parameter")
    allowed = visible_team_ids(entity)
    query = select(NotificationDelivery).where(NotificationDelivery.tenant_id == entity.tenant_id)
    if allowed is not None:
        from sqlalchemy import or_
        query = query.where(or_(NotificationDelivery.team_id.in_([team for team in allowed if team is not None]),
            NotificationDelivery.team_id.is_(None) if None in allowed else False))
    rows = session.exec(query.order_by(NotificationDelivery.created_at.desc(), NotificationDelivery.id.desc())
        .limit(limit)).all()
    return {"schema_version": 1, "evaluated_at": utc_string(utc_now()), "items": [{
        "id": str(row.id), "event_id": str(row.event_id), "team_id": row.team_id,
        "subscriber_id": row.subscriber_id, "destination_id": row.destination_id,
        "state": row.state, "attempts": row.attempts, "last_error_code": row.last_error_code,
        "available_at": utc_string(row.available_at), "delivered_at": utc_string(row.delivered_at),
    } for row in rows]}


@router.get("/{silence_id}", response_model=SilenceDto)
def get_silence(silence_id: UUID, entity: AuthenticatedEntity = Depends(read_identity),
    session: Session = Depends(get_session)):
    read_snapshot(session)
    return SilencesBL(session, entity).get(silence_id)


@router.post("", response_model=SilenceMutationResponse, status_code=201)
def create(command: CreateSilenceCommand, response: Response,
    entity: AuthenticatedEntity = Depends(create_identity), session: Session = Depends(get_session)):
    result, response.status_code = SilencesBL(session, entity, origin=entity.integration_origin).create(command)
    response.headers["Location"] = f"/integrations/silences/{result.result.id}"
    return result


@router.patch("/{silence_id}", response_model=SilenceMutationResponse)
def extend(silence_id: UUID, command: UpdateSilenceCommand,
    entity: AuthenticatedEntity = Depends(update_identity), session: Session = Depends(get_session)):
    return SilencesBL(session, entity, origin=entity.integration_origin).update(silence_id, command)[0]


@router.post("/{silence_id}/cancel", response_model=SilenceMutationResponse)
def cancel(silence_id: UUID, command: CancelSilenceCommand,
    entity: AuthenticatedEntity = Depends(update_identity), session: Session = Depends(get_session)):
    return SilencesBL(session, entity, origin=entity.integration_origin).cancel(silence_id, command)[0]
