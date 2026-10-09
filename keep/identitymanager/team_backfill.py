"""Classify historical alerts by zone.

Only an event's own zone establishes its historical owner. Current fingerprint
enrichments may have changed since that event and cannot establish ownership.
Events without a recognized zone remain unassigned for admin review.
Dry-run by default. Run with ``--apply`` only after reviewing its output.
"""

import argparse
from collections import defaultdict

from sqlalchemy import and_
from sqlmodel import Session, select

from keep.api.core.db import engine
from keep.api.models.db.alert import Alert, LastAlertToIncident
from keep.api.models.db.incident import Incident
from keep.identitymanager.team_policy import get_team_policy


def backfill(apply: bool = False, batch_size: int = 1000) -> dict[str, int]:
    if batch_size <= 0:
        raise ValueError("Batch size must be positive")
    policy = get_team_policy()
    if policy is None:
        raise ValueError("KEEP_TEAMS_CONFIG_FILE or KEEP_TEAMS_CONFIG is required")

    counts = {
        "alerts": 0,
        "unassigned_alerts": 0,
        "incidents": 0,
        "unassigned_incidents": 0,
    }
    with Session(engine) as session:
        last_alert_id = None
        while True:
            query = select(Alert).where(Alert.team_id.is_(None)).order_by(Alert.id)
            if last_alert_id is not None:
                query = query.where(Alert.id > last_alert_id)
            alerts = session.exec(query.limit(batch_size)).all()
            if not alerts:
                break
            last_alert_id = alerts[-1].id
            for alert in alerts:
                new_team = policy.team_for_zone((alert.event or {}).get("zone"))
                if new_team is None:
                    counts["unassigned_alerts"] += 1
                else:
                    alert.team_id = new_team
                    counts["alerts"] += 1
            session.flush()
            session.expunge_all()

        last_incident_id = None
        while True:
            query = select(Incident).order_by(Incident.id)
            if last_incident_id is not None:
                query = query.where(Incident.id > last_incident_id)
            incidents = session.exec(query.limit(batch_size)).all()
            if not incidents:
                break
            last_incident_id = incidents[-1].id
            incident_teams = defaultdict(set)
            links = session.exec(
                select(LastAlertToIncident.incident_id, Alert.team_id)
                .join(
                    Alert,
                    and_(
                        LastAlertToIncident.tenant_id == Alert.tenant_id,
                        LastAlertToIncident.fingerprint == Alert.fingerprint,
                    ),
                )
                .where(LastAlertToIncident.incident_id.in_([item.id for item in incidents]))
                .distinct()
            ).all()
            for incident_id, team_id in links:
                incident_teams[incident_id].add(team_id)

            for incident in incidents:
                teams = incident_teams[incident.id]
                if incident.team_id is not None:
                    if teams and teams != {incident.team_id}:
                        incident.team_id = None
                        counts["incidents"] += 1
                        counts["unassigned_incidents"] += 1
                    continue
                new_team = next(iter(teams)) if len(teams) == 1 else None
                if new_team is None:
                    counts["unassigned_incidents"] += 1
                if incident.team_id != new_team:
                    incident.team_id = new_team
                    counts["incidents"] += 1
            session.flush()
            session.expunge_all()
        if apply:
            session.commit()
    return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="persist the classification")
    parser.add_argument("--batch-size", type=int, default=1000, help="records per batch")
    args = parser.parse_args()
    print(backfill(apply=args.apply, batch_size=args.batch_size))
