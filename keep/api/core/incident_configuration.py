"""Read the published snapshot once per operation, across API/worker processes."""

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from functools import wraps
from weakref import WeakKeyDictionary

from fastapi import HTTPException
from sqlalchemy import inspect
from sqlmodel import Session, select

from keep.api.models.db.incident_configuration import IncidentConfiguration, ManagedIncidentResource


_UNBOUND = object()
_snapshot = ContextVar("keep_incident_configuration", default=_UNBOUND)
_tables = WeakKeyDictionary()
_snapshots = WeakKeyDictionary()


def configuration_tables_exist(engine):
    # Older test/embedded databases may not have run the additive migration yet.
    # Startup invalidates this after migration. Avoid opening a second SQLite
    # connection while an embedded caller owns the pool's single transaction.
    if engine not in _tables:
        _tables[engine] = inspect(engine).has_table(IncidentConfiguration.__tablename__)
    return _tables[engine]


def active_configuration(tenant_id=None, *, refresh=False):
    from keep.api.core import db
    from keep.api.core.dependencies import SINGLE_TENANT_UUID

    pinned = _snapshot.get()
    tenant_id = tenant_id or (pinned[0] if pinned is not _UNBOUND else SINGLE_TENANT_UUID)
    if pinned is not _UNBOUND and pinned[0] == tenant_id:
        return pinned[1]
    cached = _snapshots.get(db.engine, {})
    if not refresh and tenant_id in cached:
        return cached[tenant_id]
    if not configuration_tables_exist(db.engine):
        return None
    with Session(db.engine) as session:
        row = session.get(IncidentConfiguration, tenant_id)
        snapshot = row.snapshot if row and row.digest else None
    _snapshots.setdefault(db.engine, {})[tenant_id] = snapshot
    return snapshot


def reset_configuration_cache():
    _tables.clear()
    _snapshots.clear()


def published_configuration(engine, tenant_id, snapshot):
    _tables[engine] = True
    _snapshots.setdefault(engine, {})[tenant_id] = snapshot


@contextmanager
def configuration_scope(tenant_id=None):
    from keep.api.core.dependencies import SINGLE_TENANT_UUID

    tenant_id = tenant_id or SINGLE_TENANT_UUID
    current = _snapshot.get()
    if current is not _UNBOUND and current[0] == tenant_id:
        yield current[1]
        return
    snapshot = active_configuration(tenant_id, refresh=True)
    token = _snapshot.set((tenant_id, snapshot))
    try:
        yield snapshot
    finally:
        _snapshot.reset(token)


def configured_operation(function):
    """Pin configuration for sync operations whose argument is named tenant_id."""
    import inspect as function_inspect
    signature = function_inspect.signature(function)

    @wraps(function)
    def wrapped(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        with configuration_scope(bound.arguments.get("tenant_id")):
            return function(*args, **kwargs)
    return wrapped


def check_workflow_configuration(context_manager, supplied_session=None):
    """A queued workflow must not start a step with an unpublished/stale snapshot."""
    from keep.api.core import db
    from keep.api.core.incident_contract import ContractError

    if getattr(context_manager, "automation_operation", None):
        from keep.api.core.incident_automation import check_automation_context
        check_automation_context(context_manager)

    if not hasattr(context_manager, "configuration_digest") or not configuration_tables_exist(db.engine):
        return
    supplied = supplied_session if supplied_session is not None else getattr(context_manager, "db_session", None)
    with (nullcontext(supplied) if isinstance(supplied, Session) else Session(db.engine)) as session:
        current = session.exec(select(IncidentConfiguration.digest).where(
            IncidentConfiguration.tenant_id == context_manager.tenant_id,
        )).first()
    if current != context_manager.configuration_digest:
        if getattr(context_manager, "automation_operation", None):
            context_manager.automation_operation["cancelled_reason"] = "configuration_changed"
        raise ContractError("Workflow configuration changed; schedule against the active version")


def managed_metadata(session, tenant_id, kind, target_id):
    if not configuration_tables_exist(session.get_bind()):
        return None
    row = session.exec(select(ManagedIncidentResource).where(
        ManagedIncidentResource.tenant_id == tenant_id,
        ManagedIncidentResource.kind == kind,
        ManagedIncidentResource.target_id == str(target_id),
    )).first()
    if not row:
        return None
    return {"kind": row.kind, "logical_id": row.logical_id, "revision": row.revision,
            "digest": row.digest, "generation": row.generation, "managed": True, "retired": row.deleted}


def require_unmanaged(session, tenant_id, kind, target_id):
    metadata = managed_metadata(session, tenant_id, kind, target_id)
    if metadata:
        raise HTTPException(409, detail={"code": "iac_managed_resource", **metadata})


def configured_resources(session, tenant_id, kind, model, rows):
    """Project managed resources from the pinned immutable version.

    Tombstones retain ownership/IDs. A concurrent apply cannot introduce a new
    managed row into an operation that already pinned an older configuration.
    UI-owned rows continue to come from the existing database queries.
    """
    snapshot = active_configuration(tenant_id)
    if snapshot is None:
        return rows
    owned = session.exec(select(ManagedIncidentResource.target_id).where(
        ManagedIncidentResource.tenant_id == tenant_id,
        ManagedIncidentResource.kind == kind,
    )).all()
    result = [row for row in rows if str(row.id) not in owned]
    for entry in snapshot["resources"]:
        if entry["kind"] == kind and entry.get("runtime") is not None:
            result.append(model.model_validate(entry["runtime"]))
    return result


class IncidentConfigurationMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        with configuration_scope():
            await self.app(scope, receive, send)
