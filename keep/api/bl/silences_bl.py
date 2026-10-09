"""Persistent silence commands. A rule, its audit and its receipt commit together."""

import base64
import hashlib
import json
from datetime import datetime
from uuid import UUID, uuid4

import celpy
from fastapi import HTTPException
from sqlalchemy import and_, or_, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from keep.api.core.config import config
from keep.api.models.db.alert import Alert, LastAlertToIncident
from keep.api.models.db.incident import Incident
from keep.api.models.db.silence import Silence, SilenceCommand, SilenceEvent
from keep.api.models.db.user import User
from keep.api.models.silence import (
    SilenceActor, SilenceDto, SilenceListResponse, SilenceMutationResponse,
    utc_now, utc_string,
)
from keep.api.utils.cel_utils import preprocess_cel_expression
from keep.identitymanager.authenticatedentity import AuthenticatedEntity
from keep.identitymanager.rbac import get_role_by_role_name
from keep.identitymanager.team_access import (
    has_global_access, require_team_access, visible_team_ids,
)
from keep.identitymanager.team_policy import get_team_policy, is_team_scoping_active


def fail(status, code, message, field=None, revision=None):
    raise HTTPException(status_code=status, detail={
        "code": code, "message": message, "field": field,
        "current_revision": revision,
    })


def state_at(rule: Silence, now: datetime) -> str:
    if rule.cancelled_at is not None:
        return "cancelled"
    if now < rule.starts_at:
        return "scheduled"
    if rule.ends_at is not None and now >= rule.ends_at:
        return "expired"
    return "active"


def resource(rule: Silence, now: datetime) -> SilenceDto:
    def actor(value):
        # Old AM mirrors used an invalid system/name actor; keep them readable
        # while the next poll repairs their stored representation.
        if rule.origin == "alertmanager" and value.get("kind") == "system":
            return {"kind": "service", "subject": "alertmanager", "issuer": None,
                    "display_name": value.get("name", "Alertmanager")}
        return value
    return SilenceDto(
        id=rule.id, revision=rule.revision, tenant_id=rule.tenant_id,
        team_id=rule.team_id, selector=rule.selector,
        starts_at=utc_string(rule.starts_at), ends_at=utc_string(rule.ends_at),
        comment=rule.comment, created_by=actor(rule.created_by), updated_by=actor(rule.updated_by),
        created_at=utc_string(rule.created_at), updated_at=utc_string(rule.updated_at),
        cancelled_at=utc_string(rule.cancelled_at), origin=rule.origin,
        correlation_id=rule.correlation_id, state=state_at(rule, now),
        evaluated_at=utc_string(now),
        read_only=rule.origin == "alertmanager",
        synchronization=[{"source_id": source, "state": context.get("state", "pending"),
            "reason": context.get("reason")} for source, context in (rule.external_context or {}).items()],
    )


def compile_filter(expression: str, *, alertmanager=False):
    try:
        env = celpy.Environment()
        functions = None
        if alertmanager:
            import re2
            functions = {"matches": lambda value, pattern: celpy.celtypes.BoolType(re2.search(str(pattern), str(value)) is not None)}
        return env.program(env.compile(preprocess_cel_expression(expression)), functions=functions)
    except (celpy.CELParseError, ValueError, RecursionError):
        fail(422, "invalid_selector", "Invalid CEL expression", "selector.cel")


def check_alert_access(session, entity, fingerprint, *, for_write=False):
    owners = session.exec(select(Alert.team_id).where(
        Alert.tenant_id == entity.tenant_id, Alert.fingerprint == fingerprint,
    ).distinct()).all()
    if not owners:
        fail(404, "not_found", "Not found")
    for owner in owners:
        require_team_access(entity, owner, for_write=for_write)
    return owners


def check_incident_access(session, entity, incident_id, *, for_write=False):
    incident = session.exec(select(Incident).where(
        Incident.tenant_id == entity.tenant_id, Incident.id == incident_id,
    )).first()
    if incident is None:
        fail(404, "not_found", "Not found")
    require_team_access(entity, incident.team_id, for_write=for_write)
    if not has_global_access(entity) and (for_write or visible_team_ids(entity) is not None):
        owners = session.exec(select(Alert.team_id).select_from(LastAlertToIncident).join(
            Alert, and_(Alert.tenant_id == LastAlertToIncident.tenant_id,
                        Alert.fingerprint == LastAlertToIncident.fingerprint),
        ).where(LastAlertToIncident.tenant_id == entity.tenant_id,
                LastAlertToIncident.incident_id == incident.id).distinct()).all()
        if any(owner != incident.team_id for owner in owners):
            if for_write and visible_team_ids(entity) is None:
                fail(403, "forbidden", "Incident contains data from multiple teams")
            fail(404, "not_found", "Not found")
    return incident


class SilencesBL:
    def __init__(self, session: Session, entity: AuthenticatedEntity, now=None, *,
                 commit=True, origin="keep-api"):
        self.session = session
        self.entity = entity
        self.fixed_clock = now is not None
        self.now = now or utc_now()
        self.commit = commit
        self.origin = origin

    def _actor(self):
        if hasattr(self.entity, "verified_silence_actor"):
            return self.entity.verified_silence_actor
        if self.entity.api_key_name:
            return SilenceActor(kind="service", subject=self.entity.api_key_name,
                                issuer=None, display_name=self.entity.api_key_name)
        user = self.session.exec(select(User).where(
            User.tenant_id == self.entity.tenant_id, User.username == self.entity.email,
        )).first()
        if user is None:
            fail(401, "unauthenticated", "Stable user identity is required")
        # SQLite may reuse an integer ID after deletion; distinguish account lifetimes.
        subject = "keep-user:" + self._hash([user.tenant_id, user.id, utc_string(user.created_at)])
        return SilenceActor(kind="user", subject=subject,
                            issuer=None, display_name=self.entity.email)

    def _actor_scope(self, actor):
        return self._hash([actor.kind, actor.issuer, actor.subject, self.entity.api_key_name])

    @staticmethod
    def _hash(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _check_selector(self, team_id, selector, *, for_write=False, allow_missing=False):
        require_team_access(self.entity, team_id, for_write=for_write)
        kind = selector["kind"]
        if kind == "filter":
            if for_write:
                compile_filter(selector["cel"])
            return
        targets = selector["fingerprints"] if kind == "alert" else selector["incident_ids"]
        for target in targets:
            try:
                if kind == "alert":
                    owners = check_alert_access(self.session, self.entity, target, for_write=for_write)
                    if for_write and not has_global_access(self.entity) and any(owner != team_id for owner in owners):
                        fail(403 if visible_team_ids(self.entity) is None else 404,
                             "forbidden" if visible_team_ids(self.entity) is None else "not_found", "Not found")
                    matches = team_id in owners
                else:
                    incident = check_incident_access(self.session, self.entity, UUID(str(target)), for_write=for_write)
                    matches = incident.team_id == team_id
            except HTTPException as exc:
                # An existing rule must remain readable/cancellable after its target is removed.
                if allow_missing and exc.status_code == 404:
                    model, identifier = (Alert, Alert.fingerprint) if kind == "alert" else (Incident, Incident.id)
                    existing = self.session.exec(select(model.id).where(
                        model.tenant_id == self.entity.tenant_id,
                        identifier == (target if kind == "alert" else UUID(str(target))),
                    ).limit(1)).first()
                    if existing is None or has_global_access(self.entity):
                        continue
                raise
            if not matches and not allow_missing:
                fail(422 if for_write else 404,
                     "invalid_selector" if for_write else "not_found",
                     "Targets must belong to the selected team" if for_write else "Not found",
                     "selector" if for_write else None)

    def _get(self, silence_id, *, for_write=False, lock=False):
        query = select(Silence).where(Silence.tenant_id == self.entity.tenant_id, Silence.id == silence_id)
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        rule = self.session.exec(query).first()
        if rule is None:
            fail(404, "not_found", "Not found")
        self._check_selector(rule.team_id, rule.selector, for_write=for_write, allow_missing=True)
        return rule

    def get(self, silence_id):
        return resource(self._get(silence_id), self.now)

    def list(self, *, state=None, team_id=None, filter_team=False, limit=50, cursor=None):
        allowed = visible_team_ids(self.entity)
        context = self._hash([self.entity.tenant_id, self.entity.email, self.entity.api_key_name,
                              self.entity.role, sorted(allowed, key=lambda team: team or "") if allowed is not None else None,
                              state, team_id, filter_team])
        query = select(Silence).where(Silence.tenant_id == self.entity.tenant_id)
        if allowed is not None:
            query = query.where(or_(Silence.team_id.in_([team for team in allowed if team is not None]),
                Silence.team_id.is_(None) if None in allowed else False))
        if filter_team:
            query = query.where(Silence.team_id == team_id)
        if state == "cancelled":
            query = query.where(Silence.cancelled_at.is_not(None))
        elif state:
            query = query.where(Silence.cancelled_at.is_(None))
            if state == "scheduled":
                query = query.where(Silence.starts_at > self.now)
            elif state == "active":
                query = query.where(Silence.starts_at <= self.now,
                                    or_(Silence.ends_at.is_(None), Silence.ends_at > self.now))
            else:
                query = query.where(Silence.starts_at <= self.now, Silence.ends_at <= self.now)
        after = None
        if cursor:
            try:
                if len(cursor) > 4096:
                    raise ValueError()
                data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
                if not isinstance(data, dict) or set(data) != {"at", "id", "scope"} or any(
                    not isinstance(value, str) for value in data.values()
                ) or data["scope"] != context:
                    raise ValueError()
                after = (datetime.fromisoformat(data["at"]), UUID(data["id"]))
                if after[0].tzinfo is not None:
                    raise ValueError()
            except (ValueError, TypeError, KeyError):
                fail(422, "validation_error", "Invalid cursor", "cursor")
        items = []
        while len(items) <= limit:
            batch = query
            if after:
                batch = batch.where(or_(Silence.created_at < after[0], and_(
                    Silence.created_at == after[0], Silence.id < after[1],
                )))
            rows = self.session.exec(batch.order_by(Silence.created_at.desc(), Silence.id.desc()).limit(limit + 1)).all()
            if not rows:
                break
            for rule in rows:
                after = (rule.created_at, rule.id)
                try:
                    self._check_selector(rule.team_id, rule.selector, allow_missing=True)
                except HTTPException as exc:
                    if exc.status_code in (403, 404):
                        continue
                    raise
                items.append(resource(rule, self.now))
                if len(items) > limit:
                    break
            if len(rows) < limit + 1:
                break
        next_cursor = None
        if len(items) > limit:
            last = items[limit - 1]
            next_cursor = base64.urlsafe_b64encode(json.dumps({
                "at": datetime.fromisoformat(last.created_at[:-1]).isoformat(),
                "id": str(last.id), "scope": context,
            }, separators=(",", ":")).encode()).decode().rstrip("=")
        return SilenceListResponse(evaluated_at=utc_string(self.now), items=items[:limit], next_cursor=next_cursor)

    def create(self, command):
        return self._mutate("POST", None, command)

    def update(self, silence_id, command):
        return self._mutate("PATCH", silence_id, command)

    def cancel(self, silence_id, command):
        return self._mutate("CANCEL", silence_id, command)

    def _replay(self, receipt, digest):
        self._get(receipt.silence_id, for_write=True)
        original = receipt.response["result"]
        self._check_selector(original["team_id"], original["selector"], for_write=True, allow_missing=True)
        if receipt.command_hash != digest:
            fail(409, "idempotency_conflict", "Command ID has already been used")
        response = SilenceMutationResponse.parse_obj(receipt.response)
        response.replayed = True
        return response, receipt.http_status

    def check_write_permission(self, scope):
        if config("KEEP_READ_ONLY", default=False, cast=bool):
            fail(403, "forbidden", "Keep is read only")
        if not get_role_by_role_name(self.entity.role).has_scopes([scope]):
            fail(403, "forbidden", "Insufficient permissions")
        if hasattr(self.entity, "service_scopes") and scope not in self.entity.service_scopes:
            fail(403, "forbidden", "Insufficient integration permissions")

    def _mutate(self, method, silence_id, command):
        self.check_write_permission("write:silence" if method == "POST" else "update:silence")
        # SQLite has no SELECT FOR UPDATE. Reserve its writer before the first read.
        if self.session.get_bind().dialect.name == "sqlite" and not self.session.in_transaction():
            self.session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        actor_scope = digest = None
        try:
            actor = self._actor()
            actor_scope = self._actor_scope(actor)
            body = json.loads(command.json(exclude_unset=True))
            digest = self._hash([method, str(silence_id) if silence_id else None, body])
            receipt_key = (self.entity.tenant_id, actor_scope, command.client_request_id)
            receipt = self.session.get(SilenceCommand, receipt_key)
            if receipt:
                return self._replay(receipt, digest)
            rule = None
            if method == "POST":
                team_id = command.team_id
                if team_id is not None:
                    policy = get_team_policy() if is_team_scoping_active() else None
                    if policy is None or team_id not in policy.teams:
                        fail(422, "validation_error", "Unknown team", "team_id")
                selector = json.loads(command.selector.json())
                self._check_selector(team_id, selector, for_write=True)
                if not self.fixed_clock:
                    self.now = utc_now()
                starts = datetime.fromisoformat(command.starts_at[:-1]) if command.starts_at else self.now
                if starts < self.now:
                    fail(422, "invalid_time", "Start cannot be in the past", "starts_at")
                ends = datetime.fromisoformat(command.ends_at[:-1]) if command.ends_at else None
                self._validate_end(starts, ends)
                rule = Silence(
                    tenant_id=self.entity.tenant_id, team_id=team_id, selector=selector,
                    starts_at=starts, ends_at=ends, comment=command.comment,
                    created_by=actor.dict(), updated_by=actor.dict(),
                    created_at=self.now, updated_at=self.now, origin=self.origin,
                    correlation_id=command.correlation_id,
                    last_event_state="scheduled" if starts > self.now else "active",
                )
                self.session.add(rule)
                self.session.flush()
                event_type, changed, http_status = "created", True, 201
            else:
                rule = self._get(silence_id, for_write=True, lock=True)
                if rule.origin == "alertmanager":
                    fail(409, "externally_managed_silence", "Manage this shared silence in Alertmanager")
                # A duplicate update can have waited for the original command's row lock.
                receipt = self.session.exec(select(SilenceCommand).where(
                    SilenceCommand.tenant_id == self.entity.tenant_id,
                    SilenceCommand.actor_scope == actor_scope,
                    SilenceCommand.client_request_id == command.client_request_id,
                ).with_for_update()).first()
                if receipt:
                    return self._replay(receipt, digest)
                if not self.fixed_clock:
                    self.now = utc_now()
                if command.expected_revision != rule.revision:
                    fail(409, "revision_conflict", "Revision has changed", revision=rule.revision)
                current_state = state_at(rule, self.now)
                changes = {}
                if method == "CANCEL":
                    if current_state == "expired":
                        fail(409, "invalid_state", "Expired silence cannot be cancelled")
                    if current_state != "cancelled":
                        changes = {"cancelled_at": self.now, "last_event_state": "cancelled"}
                    event_type = "cancelled"
                else:
                    if current_state not in ("scheduled", "active"):
                        fail(409, "invalid_state", "Only scheduled or active silences can be updated")
                    proposed = json.loads(command.changes.json(exclude_unset=True))
                    selector = proposed.get("selector", rule.selector)
                    self._check_selector(rule.team_id, selector, for_write=True,
                                         allow_missing="selector" not in proposed)
                    starts = datetime.fromisoformat(proposed["starts_at"][:-1]) if "starts_at" in proposed else rule.starts_at
                    if starts != rule.starts_at:
                        if current_state == "active":
                            fail(422, "invalid_time", "Active start cannot be changed", "changes.starts_at")
                        if starts < self.now:
                            fail(422, "invalid_time", "Start cannot be in the past", "changes.starts_at")
                    ends = rule.ends_at
                    if "ends_at" in proposed:
                        ends = datetime.fromisoformat(proposed["ends_at"][:-1]) if proposed["ends_at"] else None
                    self._validate_end(starts, ends)
                    proposed.update(starts_at=starts, ends_at=ends)
                    changes = {name: value for name, value in proposed.items() if getattr(rule, name) != value}
                    event_type = "updated"
                changed, http_status = bool(changes), 200
                if changed:
                    next_revision = rule.revision + 1
                    new_correlation_id = command.correlation_id if command.correlation_id is not None else rule.correlation_id
                    changes.update(revision=next_revision, updated_by=actor.dict(), updated_at=self.now,
                                   correlation_id=new_correlation_id)
                    result = self.session.exec(update(Silence).where(
                        Silence.id == rule.id, Silence.tenant_id == self.entity.tenant_id,
                        Silence.revision == command.expected_revision,
                    ).values(**changes).execution_options(synchronize_session=False))
                    if result.rowcount != 1:
                        fail(409, "revision_conflict", "Revision has changed")
                    self.session.refresh(rule)
            if changed:
                rule.last_event_state = state_at(rule, self.now)
                self.session.add(rule)
            response = SilenceMutationResponse(client_request_id=command.client_request_id,
                                               replayed=False, result=resource(rule, self.now))
            if changed:
                event_id = uuid4()
                payload = {
                    "schema_version": 1, "event_id": str(event_id),
                    "event_type": f"silence.{event_type}", "occurred_at": utc_string(self.now),
                    "effective_at": utc_string(rule.starts_at if method == "POST" else self.now),
                    "silence_id": str(rule.id), "revision": rule.revision,
                    "tenant_id": rule.tenant_id, "team_id": rule.team_id,
                    "origin": self.origin, "correlation_id": rule.correlation_id,
                    "client_request_id": str(command.client_request_id), "actor": actor.dict(),
                    "reason": command.reason if method == "CANCEL" else "",
                    "resource": json.loads(response.result.json()),
                }
                from keep.api.bl.silences_delivery_bl import append_silence_event

                append_silence_event(self.session, SilenceEvent(event_id=event_id, tenant_id=rule.tenant_id,
                    team_id=rule.team_id, silence_id=rule.id, revision=rule.revision,
                    event_type=payload["event_type"], occurred_at=self.now, payload=payload))
            self.session.add(SilenceCommand(tenant_id=self.entity.tenant_id,
                actor_scope=actor_scope, client_request_id=command.client_request_id,
                command_hash=digest, silence_id=rule.id, http_status=http_status,
                response=json.loads(response.json()), created_at=self.now))
            if self.commit:
                self.session.commit()
            else:
                self.session.flush()
            return response, http_status
        except IntegrityError:
            # A concurrent identical command may have committed while we held another row.
            self.session.rollback()
            if actor_scope and digest:
                receipt = self.session.get(SilenceCommand, (self.entity.tenant_id, actor_scope, command.client_request_id))
                if receipt:
                    return self._replay(receipt, digest)
            raise
        except Exception:
            self.session.rollback()
            raise

    def _validate_end(self, starts, ends):
        if ends is not None and (ends <= starts or ends <= self.now):
            fail(422, "invalid_time", "End must be after start and current time", "ends_at")
