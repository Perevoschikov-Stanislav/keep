import datetime
import logging
import os
from pathlib import Path
from contextlib import nullcontext

from sqlmodel import Session, select

import keep.api.core.db as db
from keep.api.models.db.mapping import MappingRule

logger = logging.getLogger(__name__)

KEEP_MAPPINGS_DIRECTORY_ENV_VAR = "KEEP_MAPPINGS_DIRECTORY"
SYSTEM_ACTOR = "system"


def provision_mapping_rules_from_env(tenant_id: str, session=None):
    """Validate the whole legacy directory, then publish in one transaction.

    Missing input retains existing resources. Reviewed adoption/deletion uses
    IncidentPolicies; the legacy loader never claims a UI-owned resource.
    """
    from keep.api.bl.incident_provisioning import resource_values
    from keep.api.core.incident_contract import read_yaml, require
    from keep.api.core.incident_configuration import managed_metadata
    mappings_dir = os.environ.get(KEEP_MAPPINGS_DIRECTORY_ENV_VAR)
    if not mappings_dir or not os.path.isdir(mappings_dir):
        logger.info("Mapping input unavailable; retaining existing configuration")
        return
    manifests = [(path, resource_values("mappings", read_yaml(Path(path))))
                 for path in _collect_manifest_paths(mappings_dir)]
    require(len({item["name"] for _, item in manifests}) == len(manifests), "mappings", "duplicate mapping name")
    with (Session(db.engine) if session is None else nullcontext(session)) as current_session:
        with (current_session.begin() if session is None else nullcontext()):
            session = current_session
            for path, values in manifests:
                matches = session.exec(select(MappingRule).where(MappingRule.tenant_id == tenant_id,
                                                                 MappingRule.name == values["name"]).with_for_update()).all()
                require(len(matches) <= 1, "mappings", "ambiguous existing name")
                rule = matches[0] if matches else None
                require(rule is None or rule.is_provisioned, "mappings", "name collision; explicit adoption required")
                require(rule is None or managed_metadata(session, tenant_id, "mappings", rule.id) is None,
                        "mappings", "resource belongs to IncidentPolicies")
                if rule is None:
                    rule = MappingRule(tenant_id=tenant_id, created_by=SYSTEM_ACTOR, **values)
                elif all(getattr(rule, key) == value for key, value in values.items()) and rule.provisioned_file == path:
                    continue
                else:
                    for key, value in values.items():
                        setattr(rule, key, value)
                    rule.updated_by = SYSTEM_ACTOR
                    rule.last_updated_at = datetime.datetime.now(datetime.timezone.utc)
                rule.is_provisioned, rule.provisioned_file = True, path
                session.add(rule)
                session.flush()


def _collect_manifest_paths(mappings_dir: str) -> list[str]:
    """Return sorted absolute paths of YAML manifests in the directory.

    Paths are normalized via os.path.abspath so set-membership comparison
    against MappingRule.provisioned_file (also stored as abspath) stays stable
    across runs even if cwd or KEEP_MAPPINGS_DIRECTORY shape (relative vs
    absolute) changes between restarts.
    """
    abs_dir = os.path.abspath(mappings_dir)
    paths = []
    for filename in sorted(os.listdir(abs_dir)):
        if filename.endswith((".yaml", ".yml")):
            paths.append(os.path.join(abs_dir, filename))
        else:
            logger.info("Skipping non-YAML file %s in mappings directory", filename)
    return paths
