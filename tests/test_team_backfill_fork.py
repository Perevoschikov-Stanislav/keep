"""Run in the Keep API image: python tests/test_team_backfill_fork.py."""

import unittest
from uuid import UUID

from sqlmodel import Session, select

import keep.api.routes.alerts  # Register the models with SQLModel.
from keep.api.models.db.alert import Alert, AlertEnrichment, LastAlertToIncident
from keep.api.models.db.incident import Incident
from keep.api.models.db.tenant import Tenant
from keep.identitymanager.team_backfill import backfill
from tests.team_fork_test_case import TeamDatabaseTestCase


def event(fingerprint, **fields):
    return Alert(
        tenant_id="tenant", provider_type="prometheus", provider_id="am",
        event={"name": fingerprint, **fields}, fingerprint=fingerprint,
    )


class TeamBackfillTest(TeamDatabaseTestCase):
    def setUp(self):
        super().setUp()
        with Session(self.engine) as session:
            session.add(Tenant(id="tenant", name="Tenant"))
            session.add_all((
                AlertEnrichment(
                    tenant_id="tenant", alert_fingerprint="legacy",
                    enrichments={"zone": "ALPHA"},
                ),
                AlertEnrichment(
                    tenant_id="tenant", alert_fingerprint="moved",
                    enrichments={"zone": "ALPHA"},
                ),
                event("legacy"),
                event("legacy"),
                event("moved", zone="BETA"),
                event("orphan"),
            ))
            incident = Incident(
                tenant_id="tenant", user_summary="legacy", generated_summary="",
            )
            session.add(incident)
            session.flush()
            session.add(LastAlertToIncident(
                tenant_id="tenant", fingerprint="legacy", incident_id=incident.id,
            ))
            self.incident_id = incident.id
            session.commit()

    def teams(self):
        with Session(self.engine) as session:
            alerts = session.exec(select(Alert.fingerprint, Alert.team_id)).all()
            incident = session.get(Incident, self.incident_id)
            return sorted(alerts, key=lambda row: (row[0], row[1] or "")), incident.team_id

    def test_current_enrichment_does_not_assign_historical_ownership(self):
        expected = {
            "alerts": 1,
            "unassigned_alerts": 3,
            "incidents": 0,
            "unassigned_incidents": 1,
        }
        self.assertEqual(backfill(), expected)
        self.assertEqual(self.teams(), ([
            ("legacy", None), ("legacy", None), ("moved", None), ("orphan", None),
        ], None))

        self.assertEqual(backfill(apply=True), expected)
        self.assertEqual(self.teams(), ([
            ("legacy", None), ("legacy", None), ("moved", "beta"), ("orphan", None),
        ], None))

        with Session(self.engine) as session:
            enrichment = session.exec(
                select(AlertEnrichment).where(AlertEnrichment.alert_fingerprint == "legacy")
            ).one()
            enrichment.enrichments = {"zone": "BETA"}
            session.commit()

        self.assertEqual(backfill(apply=True), {
            "alerts": 0,
            "unassigned_alerts": 3,
            "incidents": 0,
            "unassigned_incidents": 1,
        })
        self.assertEqual(self.teams()[1], None)

    def test_batches_classify_all_events_and_preserve_ambiguous_incidents(self):
        incident_ids = []
        with Session(self.engine) as session:
            for index in range(8):
                alert = event(f"batch-{index}", zone="ALPHA" if index % 2 == 0 else "BETA")
                alert.id = UUID(int=index + 1)
                session.add(alert)
                incident = Incident(
                    id=UUID(int=index + 1), tenant_id="tenant",
                    user_summary=f"batch-{index}", generated_summary="",
                )
                session.add(incident)
                session.flush()
                session.add(LastAlertToIncident(
                    tenant_id="tenant", fingerprint=alert.fingerprint,
                    incident_id=incident.id,
                ))
                incident_ids.append(incident.id)

            # One incident links alerts classified in different batches.
            mixed = Incident(
                id=UUID(int=20), tenant_id="tenant", team_id="alpha",
                user_summary="mixed", generated_summary="",
            )
            session.add(mixed)
            session.flush()
            for index in (0, 7):
                session.add(LastAlertToIncident(
                    tenant_id="tenant", fingerprint=f"batch-{index}", incident_id=mixed.id,
                ))
            session.commit()

        expected = {
            "alerts": 9, "unassigned_alerts": 3,
            "incidents": 9, "unassigned_incidents": 2,
        }
        self.assertEqual(backfill(batch_size=2), expected)
        with Session(self.engine) as session:
            self.assertTrue(all(session.get(Incident, item).team_id is None for item in incident_ids))
            self.assertEqual(session.get(Incident, UUID(int=20)).team_id, "alpha")

        self.assertEqual(backfill(apply=True, batch_size=2), expected)
        with Session(self.engine) as session:
            self.assertEqual(
                [session.get(Incident, item).team_id for item in incident_ids],
                ["alpha", "beta"] * 4,
            )
            self.assertIsNone(session.get(Incident, UUID(int=20)).team_id)

    def test_batch_size_must_be_positive(self):
        with self.assertRaises(ValueError):
            backfill(batch_size=0)


if __name__ == "__main__":
    unittest.main()
