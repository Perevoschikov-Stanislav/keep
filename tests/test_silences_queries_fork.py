"""Effective, DTOs, SQL pagination/counts and facets must agree."""

import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from keep.api.alert_deduplicator.alert_deduplicator import AlertDeduplicator
from keep.api.bl.silences_bl import SilencesBL
from keep.api.core import alerts as alerts_core, facets as facets_core, incidents as incidents_core
from keep.api.models.alert import AlertDto
from keep.api.models.db.alert import Alert, AlertEnrichment
from keep.api.models.db.incident import Incident
from keep.api.models.facet import CreateFacetDto, FacetOptionsQueryDto
from keep.api.models.incident import IncidentDto
from keep.api.models.query import QueryDto
from keep.api.models.silence import CancelSilenceCommand, utc_string
from keep.api.tasks import process_event_task
from keep.api.utils.enrichment_helpers import convert_db_alerts_to_dto_alerts
from tests.test_silences_api_fork import NOW, SilenceDatabaseCase


class SilenceQueryTest(SilenceDatabaseCase):
    def setUp(self):
        super().setUp()
        self.clock = self.enterContext(patch("keep.api.bl.silences_evaluator.utc_now", return_value=NOW))

    def query(self, cel, *, teams=frozenset({"alpha"}), limit=1000, offset=0):
        return alerts_core.query_last_alerts("keep", QueryDto(cel=cel, limit=limit, offset=offset), teams)

    def test_reingested_api_response_does_not_persist_computed_dismiss(self):
        rule = self.create().result
        with Session(self.engine) as session:
            dto = convert_db_alerts_to_dto_alerts([session.get(Alert, self.alpha_id)], session=session)[0]
        self.assertTrue(dto.dismissed)
        events = [
            AlertDto(**{**json.loads(dto.json()), "zone": "ALPHA"}),
            AlertDto(name="Legacy", fingerprint="legacy-input", source=["test"],
                zone="ALPHA", status="firing", severity="high",
                lastReceived=utc_string(NOW), dismissed=True),
        ]
        getattr(process_event_task, "__internal_prepartion")(events, None, None)
        for index, dto in enumerate(events):
            dto.alert_hash = f"ingest-{index}"
        with patch.object(process_event_task, "EnrichmentsBl") as enrichments, \
             patch.object(process_event_task, "KEEP_CALCULATE_START_FIRING_TIME_ENABLED", False), \
             patch.object(process_event_task, "KEEP_AUDIT_EVENTS_ENABLED", False), \
             patch.object(process_event_task, "get_enrichment_with_session", return_value=None):
            enrichments.return_value.run_extraction_rules.side_effect = lambda alert: alert
            with Session(self.engine) as session:
                getattr(process_event_task, "__save_to_db")(
                    "keep", "test", session, [], events, [], "test", NOW + timedelta(minutes=1)
                )
        with Session(self.engine) as session:
            rows = session.exec(select(Alert).where(Alert.timestamp == NOW + timedelta(minutes=1))).all()
            self.assertEqual(len(rows), 2)
            for alert in rows:
                self.assertNotIn("silence", alert.event)
                self.assertEqual(alert.event["dismissed"], alert.fingerprint == "legacy-input")
                self.assertEqual(alert.event["status"], "firing")
            SilencesBL(session, self.entity, NOW).cancel(rule.id, CancelSilenceCommand(
                schema_version=1, client_request_id=uuid4(),
                expected_revision=rule.revision, reason="Finished", correlation_id=None,
            ))
            dto = convert_db_alerts_to_dto_alerts([next(row for row in rows if row.fingerprint == "a")], session=session)[0]
            self.assertFalse(dto.dismissed)
            self.assertFalse(dto.silence.silenced)

    def test_silence_metadata_does_not_change_existing_dedup_hash(self):
        self.create()
        with Session(self.engine) as session:
            original = convert_db_alerts_to_dto_alerts(
                [session.get(Alert, self.alpha_id)], session=session, with_silences=False
            )[0]
            response = convert_db_alerts_to_dto_alerts([session.get(Alert, self.alpha_id)], session=session)[0]
        response.id = original.id
        # Reconstruct the pre-normalization DTO payload: absent new response fields
        # must not change hashes saved before normalization was introduced.
        expected = hashlib.sha256(json.dumps(original.dict(exclude={
            "silence", "normalized", "normalization", "presentation", "correlation",
        }), default=str, sort_keys=True).encode()).hexdigest()
        for dto in (original, response):
            AlertDeduplicator("keep")._apply_deduplication_rule(
                dto, SimpleNamespace(id="test", ignore_fields=[]), {"a": expected}
            )
            self.assertEqual(dto.alert_hash, expected)
            self.assertTrue(dto.isFullDuplicate)

    def test_api_serialization_keeps_registry_dismiss_after_legacy_expiry(self):
        alert_id = self.alert("a", "alpha", dismissed=True, dismissUntil="2000-01-01T00:00:00.000Z")
        self.create()
        with Session(self.engine) as session:
            dto = convert_db_alerts_to_dto_alerts([session.get(Alert, alert_id)], session=session)[0]
        self.assertTrue(dto.dismissed)
        app = FastAPI()

        @app.get("/alert", response_model=AlertDto)
        def get_alert():
            return dto

        with TestClient(app) as client:
            response = client.get("/alert")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["silence"]["silenced"])
        self.assertTrue(response.json()["dismissed"])
        self.assertEqual(response.json()["status"], "firing")

    def test_alert_dto_and_filters_facets_obey_same_tenant_team_and_clock(self):
        self.create()
        self.create(self.command(team_id="beta", selector={"kind": "alert", "fingerprints": ["b"]}), entity=self.admin)
        self.alert("a", "alpha", status="resolved")
        self.alert("clean", "alpha")
        self.alert("a", "alpha", tenant="another")
        with Session(self.engine) as session:
            alert = session.get(Alert, self.alpha_id)
            original = alert.event.copy()
            dto = convert_db_alerts_to_dto_alerts([alert], session=session)[0]
            self.assertTrue(dto.dismissed)
            self.assertTrue(dto.silence.silenced)
            self.assertEqual(dto.status, "firing")
            self.assertEqual(alert.event, original)
            session.commit()
        for cel in ("dismissed == true", "silence.silenced == true", "alert.dismissed == true", "silence.coverage == 'full'"):
            with self.subTest(cel=cel):
                rows, total = self.query(cel)
                self.assertEqual(total, 1)
                self.assertEqual([row.fingerprint for row in rows], ["a"])
        rows, total = alerts_core.query_last_alerts("another", QueryDto(cel="dismissed == true"))
        self.assertEqual((len(rows), total), (0, 0))
        facet = next(facet for facet in alerts_core.static_facets if facet.property_path == "dismissed")
        options = alerts_core.get_alert_facets_data("keep", FacetOptionsQueryDto(cel="", facet_queries={facet.id: ""}), frozenset({"alpha"}))[facet.id]
        self.assertEqual({option.value: option.matches_count for option in options}, {True: 1, False: 1})
        self.clock.return_value = NOW + timedelta(hours=1)
        self.assertEqual(self.query("dismissed == true")[1], 0)
        with Session(self.engine) as session:
            alert = session.get(Alert, self.alpha_id)
            self.assertFalse(convert_db_alerts_to_dto_alerts([alert], session=session)[0].dismissed)

    def test_quoted_fingerprint_pagination_legacy_and_custom_silence_facet(self):
        fingerprint = "quote' OR 1=1 --"
        self.alert(fingerprint, "alpha")
        self.alert("legacy", "alpha", dismissed=True, dismissUntil="2100-01-01T00:00:00.000Z")
        self.create(self.command({"kind": "alert", "fingerprints": ["a", fingerprint]}))
        rows, total = self.query("dismissed == true", limit=1, offset=1)
        self.assertEqual((len(rows), total), (1, 3))
        self.assertEqual(self.query("silence.silenced == true")[1], 2)
        facet = facets_core.create_facet("keep", "alert", CreateFacetDto(name="Silenced", property_path="silence.silenced"))
        options = alerts_core.get_alert_facets_data("keep", FacetOptionsQueryDto(cel="dismissed == true", facet_queries={facet.id: ""}), frozenset({"alpha"}))[facet.id]
        self.assertEqual({option.value: option.matches_count for option in options}, {True: 2, False: 1})

    def test_incident_partial_and_full_dto_query_and_facets(self):
        self.alert("a2", "alpha")
        partial = self.incident(["a", "a2"])
        full = self.incident(["a"])
        self.incident(["b"], team="beta")
        self.create()
        with Session(self.engine) as session:
            for incident_id, silenced, coverage in ((partial, False, "partial"), (full, True, "full")):
                dto = IncidentDto.from_db_incident(session.get(Incident, incident_id), session=session)
                self.assertEqual((dto.dismissed, dto.silence.coverage), (silenced, coverage))
        for cel, expected in (("dismissed == true", full), ("silence.coverage == 'partial'", partial), ("alert.silence.silenced == true", None)):
            rows, total = incidents_core.get_last_incidents_by_cel("keep", cel=cel, allowed_team_ids=frozenset({"alpha"}))
            self.assertEqual(total, 2 if expected is None else 1)
            if expected:
                self.assertEqual([row.id for row in rows], [expected])
        facet = facets_core.create_facet("keep", "incident", CreateFacetDto(name="Silenced", property_path="silence.silenced"))
        options = incidents_core.get_incident_facets_data("keep", None, FacetOptionsQueryDto(cel="", facet_queries={facet.id: ""}), frozenset({"alpha"}))[facet.id]
        self.assertEqual({option.value: option.matches_count for option in options}, {True: 1, False: 1})

    def test_until_filter_and_silence_sort_use_derived_values(self):
        self.create()
        self.alert("clean", "alpha")
        rows, total = self.query("silence.silenced_until > '2026-10-04T12:30:00Z'")
        self.assertEqual(total, 1)
        self.assertEqual([row.fingerprint for row in rows], ["a"])
        rows, _ = alerts_core.query_last_alerts("keep", QueryDto(cel="", sort_by="silence.silenced", sort_dir="desc"), frozenset({"alpha"}))
        self.assertEqual(rows[0].fingerprint, "a")

    def test_filter_candidates_explicit_empty_incident_and_legacy_incident(self):
        self.create(self.command({"kind": "filter", "cel": "status == 'firing'"}))
        self.assertEqual(self.query("silence.silenced == true")[1], 1)
        empty = self.incident([])
        legacy = self.incident([])
        self.create(self.command({"kind": "incident", "incident_ids": [str(empty)]}))
        with Session(self.engine) as session:
            session.add(AlertEnrichment(tenant_id="keep", alert_fingerprint=str(legacy), enrichments={"dismissed": True}))
            session.commit()
        rows, total = incidents_core.get_last_incidents_by_cel("keep", cel="silence.silenced == true", allowed_team_ids=frozenset({"alpha"}))
        self.assertEqual((total, [row.id for row in rows]), (1, [empty]))
        self.assertEqual(incidents_core.get_last_incidents_by_cel("keep", cel="dismissed == true", allowed_team_ids=frozenset({"alpha"}))[1], 2)
