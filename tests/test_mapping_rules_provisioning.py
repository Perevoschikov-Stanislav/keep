"""Tests for keep.api.bl.mapping_rules_provisioning.provision_mapping_rules_from_env.

Mirrors the structure of tests/test_workflowstore.py — uses real in-memory SQLite
sessions (via the `db_session` fixture from tests/conftest.py) rather than
patching DB helpers, because the provisioning module operates on SQLModel
sessions directly (same pattern as the existing MappingRule REST routes).
"""

import datetime
import os

import pytest
from fastapi import HTTPException
from sqlmodel import Session, select

import keep.api.core.db as db
from keep.api.bl.mapping_rules_provisioning import provision_mapping_rules_from_env
from keep.api.core.dependencies import SINGLE_TENANT_UUID
from keep.api.models.db.mapping import MappingRule, MappingRuleUpdateDtoIn
from keep.api.routes.mapping import delete_rule, update_rule
from keep.identitymanager.authenticatedentity import AuthenticatedEntity

FIXTURE_DIR_ONE = "./tests/provision/mapping_rules_1"
FIXTURE_DIR_TWO = "./tests/provision/mapping_rules_2"
FIXTURE_DIR_INVALID = "./tests/provision/mapping_rules_invalid"
FIXTURE_DIR_EMPTY = "./tests/provision/mapping_rules_empty"
FIXTURE_DIR_MISSING = "./tests/provision/mapping_rules_does_not_exist"
FIXTURE_DIR_SAME_NAME = "./tests/provision/mapping_rules_same_name"
FIXTURE_DIR_WITH_NOISE = "./tests/provision/mapping_rules_with_noise"


def _all_mapping_rules(tenant_id=SINGLE_TENANT_UUID) -> list[MappingRule]:
    with Session(db.engine) as session:
        return session.exec(
            select(MappingRule).where(MappingRule.tenant_id == tenant_id)
        ).all()


def _provisioned_mapping_rules(tenant_id=SINGLE_TENANT_UUID) -> list[MappingRule]:
    with Session(db.engine) as session:
        return session.exec(
            select(MappingRule).where(
                MappingRule.tenant_id == tenant_id,
                MappingRule.is_provisioned == True,  # noqa: E712
            )
        ).all()


def test_creates_new_rule(monkeypatch, db_session):
    """Empty DB + manifest dir → rule is created and marked provisioned."""
    monkeypatch.setenv("KEEP_MAPPINGS_DIRECTORY", FIXTURE_DIR_ONE)

    provision_mapping_rules_from_env(SINGLE_TENANT_UUID)

    rules = _provisioned_mapping_rules()
    assert len(rules) == 1
    rule = rules[0]
    assert rule.name == "example-prometheus-mapping"
    assert rule.is_provisioned is True
    assert rule.provisioned_file.endswith("prometheus-alerts.yaml")
    assert rule.type == "csv"
    assert rule.matchers == [["namespace"]]
    assert len(rule.rows) == 2
    assert rule.rows[0]["namespace"] == "monitoring"
    assert rule.created_by == "system"


def test_provisions_multiple_rules(monkeypatch, db_session):
    """Two manifests in dir → both rules provisioned."""
    monkeypatch.setenv("KEEP_MAPPINGS_DIRECTORY", FIXTURE_DIR_TWO)

    provision_mapping_rules_from_env(SINGLE_TENANT_UUID)

    rules = _provisioned_mapping_rules()
    names = sorted(r.name for r in rules)
    assert names == ["example-cloudwatch-mapping", "example-prometheus-mapping"]


def test_is_idempotent(monkeypatch, db_session):
    """Running provisioning twice does not create duplicates."""
    monkeypatch.setenv("KEEP_MAPPINGS_DIRECTORY", FIXTURE_DIR_TWO)

    provision_mapping_rules_from_env(SINGLE_TENANT_UUID)
    first_ids = sorted(r.id for r in _provisioned_mapping_rules())
    assert len(first_ids) == 2

    provision_mapping_rules_from_env(SINGLE_TENANT_UUID)
    second_ids = sorted(r.id for r in _provisioned_mapping_rules())

    assert first_ids == second_ids


def test_rejects_ui_rule_with_matching_name(monkeypatch, db_session):
    with Session(db.engine) as session:
        rule = MappingRule(tenant_id=SINGLE_TENANT_UUID, name="example-prometheus-mapping",
                           priority=99, matchers=[["namespace"]], rows=[{"namespace": "manual"}])
        session.add(rule)
        session.commit()
    monkeypatch.setenv("KEEP_MAPPINGS_DIRECTORY", FIXTURE_DIR_ONE)
    with pytest.raises(ValueError, match="explicit adoption"):
        provision_mapping_rules_from_env(SINGLE_TENANT_UUID)
    assert _all_mapping_rules()[0].priority == 99
    assert not _all_mapping_rules()[0].is_provisioned


def test_duplicate_names_reject_entire_directory(monkeypatch, db_session):
    monkeypatch.setenv("KEEP_MAPPINGS_DIRECTORY", FIXTURE_DIR_SAME_NAME)
    with pytest.raises(ValueError, match="duplicate mapping name"):
        provision_mapping_rules_from_env(SINGLE_TENANT_UUID)
    assert _all_mapping_rules() == []


def test_non_yaml_files_in_directory_are_ignored(monkeypatch, db_session):
    """Non-`.yaml`/`.yml` files (e.g. README, .gitkeep, .txt) in the directory
    are silently skipped — they don't raise, don't block, don't get parsed.
    """
    monkeypatch.setenv("KEEP_MAPPINGS_DIRECTORY", FIXTURE_DIR_WITH_NOISE)

    provision_mapping_rules_from_env(SINGLE_TENANT_UUID)

    rules = _provisioned_mapping_rules()
    # Only the one .yaml file in the dir produces a rule; notes.txt is skipped
    assert len(rules) == 1
    assert rules[0].name == "example-prometheus-mapping"


def test_updates_existing_provisioned_rule(monkeypatch, db_session):
    """A previously-provisioned rule gets its content refreshed from the manifest."""
    monkeypatch.setenv("KEEP_MAPPINGS_DIRECTORY", FIXTURE_DIR_ONE)
    provision_mapping_rules_from_env(SINGLE_TENANT_UUID)

    first = _provisioned_mapping_rules()[0]
    original_id = first.id

    # Pretend the DB content drifted (simulating someone editing via UI directly)
    with Session(db.engine) as session:
        rule = session.exec(
            select(MappingRule).where(MappingRule.id == original_id)
        ).first()
        rule.priority = 42
        session.add(rule)
        session.commit()

    # Re-run provisioning — should reset priority back to manifest value (0)
    provision_mapping_rules_from_env(SINGLE_TENANT_UUID)

    refreshed = _provisioned_mapping_rules()
    assert len(refreshed) == 1
    assert refreshed[0].id == original_id
    assert refreshed[0].priority == 0


def test_retains_when_manifest_file_disappears(monkeypatch, db_session):
    """A missing file retains the last configuration until explicit deletion."""
    monkeypatch.setenv("KEEP_MAPPINGS_DIRECTORY", FIXTURE_DIR_TWO)
    provision_mapping_rules_from_env(SINGLE_TENANT_UUID)
    assert len(_provisioned_mapping_rules()) == 2

    # Swap to a dir containing only one of the two manifests (by name)
    monkeypatch.setenv("KEEP_MAPPINGS_DIRECTORY", FIXTURE_DIR_ONE)
    provision_mapping_rules_from_env(SINGLE_TENANT_UUID)

    remaining = _provisioned_mapping_rules()
    assert len(remaining) == 2


def test_retains_all_when_env_unset(monkeypatch, db_session):
    """Unsetting KEEP_MAPPINGS_DIRECTORY retains all currently-provisioned rules."""
    monkeypatch.setenv("KEEP_MAPPINGS_DIRECTORY", FIXTURE_DIR_TWO)
    provision_mapping_rules_from_env(SINGLE_TENANT_UUID)
    assert len(_provisioned_mapping_rules()) == 2

    monkeypatch.delenv("KEEP_MAPPINGS_DIRECTORY")
    provision_mapping_rules_from_env(SINGLE_TENANT_UUID)

    assert len(_provisioned_mapping_rules()) == 2


def test_leaves_unrelated_ui_rules_untouched(monkeypatch, db_session):
    """A UI rule whose name does NOT match any manifest is left alone."""
    with Session(db.engine) as session:
        ui_rule = MappingRule(
            tenant_id=SINGLE_TENANT_UUID,
            name="some-ui-only-rule-not-in-manifests",
            priority=5,
            matchers=[["unrelated"]],
            type="csv",
            rows=[{"unrelated": "yes"}],
            created_by="ui-user@example.com",
            is_provisioned=False,
        )
        session.add(ui_rule)
        session.commit()
        ui_rule_id = ui_rule.id

    monkeypatch.setenv("KEEP_MAPPINGS_DIRECTORY", FIXTURE_DIR_ONE)
    provision_mapping_rules_from_env(SINGLE_TENANT_UUID)

    # UI rule still exists, still not provisioned
    with Session(db.engine) as session:
        rule = session.exec(
            select(MappingRule).where(MappingRule.id == ui_rule_id)
        ).first()
        assert rule is not None
        assert rule.is_provisioned is False
        assert rule.priority == 5

    all_rules = _all_mapping_rules()
    assert len(all_rules) == 2  # the UI rule + the provisioned one


def test_missing_directory_preserves_configuration(monkeypatch, db_session):
    monkeypatch.setenv("KEEP_MAPPINGS_DIRECTORY", FIXTURE_DIR_ONE)
    provision_mapping_rules_from_env(SINGLE_TENANT_UUID)
    monkeypatch.setenv("KEEP_MAPPINGS_DIRECTORY", FIXTURE_DIR_MISSING)
    provision_mapping_rules_from_env(SINGLE_TENANT_UUID)
    assert len(_provisioned_mapping_rules()) == 1


def test_invalid_manifest_rejects_entire_directory(monkeypatch, db_session):
    monkeypatch.setenv("KEEP_MAPPINGS_DIRECTORY", FIXTURE_DIR_INVALID)
    with pytest.raises(ValueError):
        provision_mapping_rules_from_env(SINGLE_TENANT_UUID)
    assert _provisioned_mapping_rules() == []


def test_noop_when_env_unset_and_no_provisioned_rules(monkeypatch, db_session):
    """Calling with no env and no provisioned rules in DB is a clean no-op."""
    monkeypatch.delenv("KEEP_MAPPINGS_DIRECTORY", raising=False)
    # No rules to start with
    assert len(_all_mapping_rules()) == 0

    provision_mapping_rules_from_env(SINGLE_TENANT_UUID)

    assert len(_all_mapping_rules()) == 0


def test_empty_directory_retains_existing(monkeypatch, db_session):
    """An empty mount retains existing provisioned resources."""
    monkeypatch.setenv("KEEP_MAPPINGS_DIRECTORY", FIXTURE_DIR_ONE)
    provision_mapping_rules_from_env(SINGLE_TENANT_UUID)
    assert len(_provisioned_mapping_rules()) == 1

    monkeypatch.setenv("KEEP_MAPPINGS_DIRECTORY", FIXTURE_DIR_EMPTY)
    provision_mapping_rules_from_env(SINGLE_TENANT_UUID)
    assert len(_provisioned_mapping_rules()) == 1


def test_provisioned_file_is_absolute_and_stable_across_dir_form_changes(
    monkeypatch, db_session
):
    """Stored provisioned_file is absolute; switching env var from relative to
    absolute form between runs does not cause spurious deprovisioning."""
    # First run with a RELATIVE directory
    monkeypatch.setenv("KEEP_MAPPINGS_DIRECTORY", FIXTURE_DIR_ONE)
    provision_mapping_rules_from_env(SINGLE_TENANT_UUID)

    rules = _provisioned_mapping_rules()
    assert len(rules) == 1
    original_id = rules[0].id
    assert os.path.isabs(rules[0].provisioned_file), (
        f"provisioned_file should be absolute, got {rules[0].provisioned_file}"
    )

    # Second run with the SAME directory expressed as an absolute path
    monkeypatch.setenv("KEEP_MAPPINGS_DIRECTORY", os.path.abspath(FIXTURE_DIR_ONE))
    provision_mapping_rules_from_env(SINGLE_TENANT_UUID)

    rules = _provisioned_mapping_rules()
    assert len(rules) == 1, "rule should not have been deprovisioned"
    assert rules[0].id == original_id, "rule id should be preserved"


def _seed_provisioned_rule(session: Session) -> MappingRule:
    """Insert a provisioned MappingRule directly so REST guard tests don't depend on the directory loop."""
    rule = MappingRule(
        tenant_id=SINGLE_TENANT_UUID,
        name="example-prometheus-mapping",
        description="seeded for REST guard test",
        priority=0,
        matchers=[["namespace"]],
        rows=[{"namespace": "monitoring", "team": "platform"}],
        type="csv",
        created_by="system",
        created_at=datetime.datetime.now(tz=datetime.timezone.utc),
        is_provisioned=True,
        provisioned_file="/path/to/manifest.yaml",
    )
    session.add(rule)
    session.commit()
    session.refresh(rule)
    return rule


def _entity() -> AuthenticatedEntity:
    return AuthenticatedEntity(tenant_id=SINGLE_TENANT_UUID, email="test@example.com")


def test_delete_route_rejects_provisioned_rule(db_session):
    """REST DELETE on a provisioned rule returns 409, mirroring the dedupe guard."""
    rule = _seed_provisioned_rule(db_session)

    with pytest.raises(HTTPException) as exc_info:
        delete_rule(
            rule_id=rule.id,
            authenticated_entity=_entity(),
            session=db_session,
        )

    assert exc_info.value.status_code == 409
    assert "Provisioned mapping rule cannot be deleted" in exc_info.value.detail

    # rule is still in DB — guard short-circuited before delete
    assert db_session.get(MappingRule, rule.id) is not None


def test_update_route_rejects_provisioned_rule(db_session):
    """REST PUT on a provisioned rule returns 409, mirroring the dedupe guard."""
    rule = _seed_provisioned_rule(db_session)
    original_name = rule.name

    update_payload = MappingRuleUpdateDtoIn(
        name="renamed-by-ui",
        description="ui edit attempt",
        priority=99,
        matchers=[["namespace"]],
        type="csv",
        rows=[{"namespace": "default", "team": "data"}],
    )

    with pytest.raises(HTTPException) as exc_info:
        update_rule(
            rule_id=rule.id,
            rule=update_payload,
            authenticated_entity=_entity(),
            session=db_session,
        )

    assert exc_info.value.status_code == 409
    assert "Provisioned mapping rule cannot be updated" in exc_info.value.detail

    # rule is unchanged — guard short-circuited before mutation
    db_session.refresh(rule)
    assert rule.name == original_name
