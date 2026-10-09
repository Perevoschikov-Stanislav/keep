"""Compatibility loader: an absent input never retires workflows."""

from contextlib import nullcontext
import copy
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Session, select

from keep.api.bl.incident_provisioning import IncidentProvisioning, current_values, workflow_values
from keep.api.core import db
from keep.api.core.incident_configuration import managed_metadata
from keep.api.core.incident_contract import parse_yaml, read_yaml, require
from keep.api.models.db.workflow import Workflow


def provision_workflows_from_env(tenant_id, session=None):
    directory, inline = os.environ.get("KEEP_WORKFLOWS_DIRECTORY"), os.environ.get("KEEP_WORKFLOW")
    require(not (directory and inline), "workflows", "choose one legacy input")
    if not directory and not inline:
        return []
    if directory and not Path(directory).is_dir():
        return []
    documents = [("<env>", parse_yaml(inline))] if inline else [
        (str(path.absolute()), read_yaml(path)) for path in sorted(Path(directory).iterdir())
        if path.suffix in {".yaml", ".yml"}
    ]
    parsed = []
    for path, document in documents:
        require(isinstance(document, dict), "workflows", "workflow mapping required")
        workflow = copy.deepcopy(document.get("workflow", document.get("alert", document)))
        require(isinstance(workflow, dict), "workflows", "workflow mapping required")
        workflow.setdefault("id", workflow.get("name"))
        document = {"workflow": workflow}
        parsed.append((path, document, workflow_values(document)))
    require(len({values["name"] for _, _, values in parsed}) == len(parsed), "workflows", "duplicate workflow name")
    with (Session(db.engine) if session is None else nullcontext(session)) as current_session:
        with (current_session.begin() if session is None else nullcontext()):
            session = current_session
            for path, document, values in parsed:
                matches = session.exec(select(Workflow).where(Workflow.tenant_id == tenant_id,
                                                              Workflow.name == values["name"]).with_for_update()).all()
                require(len(matches) <= 1, "workflows", "ambiguous existing name")
                target = matches[0] if matches else None
                require(target is None or target.provisioned, "workflows", "name collision; explicit adoption required")
                require(target is None or managed_metadata(session, tenant_id, "workflows", target.id) is None,
                        "workflows", "resource belongs to IncidentPolicies")
                if target and current_values("workflows", target) == values and not target.is_deleted and target.provisioned_file == path:
                    continue
                logical_id = document["workflow"]["id"]
                IncidentProvisioning(tenant_id)._write_resource(session, "workflows", target, values, "system",
                    datetime.now(timezone.utc), [{"id": logical_id, "artifact": {"path": path}}], logical_id)
    return [document["workflow"] for _, document, _ in parsed]
