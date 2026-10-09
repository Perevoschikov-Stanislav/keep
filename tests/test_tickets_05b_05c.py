import unittest
from unittest.mock import MagicMock
from uuid import uuid4

from keep.api.models.alert import AlertDto, AlertStatus, AlertSeverity
from keep.api.models.db.incident import Incident, IncidentStatus
from keep.identitymanager.authenticatedentity import AuthenticatedEntity
from keep.api.utils.alert_utils import (
    extract_service_from_alert,
    get_service_candidate_labels,
    get_exclude_services,
)
from keep.api.bl.incidents_bl import IncidentBl


class TestTickets05b05c(unittest.TestCase):
    def test_05b_prometheus_exporter_excluded_deployment_preferred(self):
        # Alert with both exporter service and deployment
        alert = {
            "name": "KubeDeploymentReplicasMismatch",
            "service": "prometheus-kube-state-metrics",
            "labels": {
                "alertname": "KubeDeploymentReplicasMismatch",
                "service": "prometheus-kube-state-metrics",
                "deployment": "web-dashboard",
                "namespace": "default",
                "pod": "prometheus-kube-state-metrics-546c89fdcc-nn697",
            },
        }
        service = extract_service_from_alert(alert)
        self.assertEqual(service, "web-dashboard")

    def test_05b_kubepodnotready_returns_pod_not_exporter(self):
        alert = {
            "name": "KubePodNotReady",
            "service": "prometheus-kube-state-metrics",
            "labels": {
                "alertname": "KubePodNotReady",
                "pod": "image-cacher-cl0sc2-1-5c75dd5c75-t8kh9",
                "namespace": "prod",
                "service": "prometheus-kube-state-metrics",
            },
        }
        service = extract_service_from_alert(alert)
        self.assertEqual(service, "image-cacher-cl0sc2-1-5c75dd5c75-t8kh9")

    def test_05b_upstream_silent_returns_upstream(self):
        alert = {
            "name": "UpstreamSilent",
            "labels": {
                "alertname": "UpstreamSilent",
                "upstream": "10.0.0.2",
                "env": "prod",
            },
        }
        service = extract_service_from_alert(alert)
        self.assertEqual(service, "10.0.0.2")

    def test_05b_kubejobfailed_returns_job_name(self):
        alert = {
            "name": "KubeJobFailed",
            "service": "prometheus-kube-state-metrics",
            "labels": {
                "alertname": "KubeJobFailed",
                "job_name": "logical-backup-orders-db-29832455",
                "container": "kube-state-metrics",
                "pod": "prometheus-kube-state-metrics-546c89fdcc-nn697",
                "service": "prometheus-kube-state-metrics",
            },
        }
        service = extract_service_from_alert(alert)
        self.assertEqual(service, "logical-backup-orders-db-29832455")

    def test_05b_cputhrottlinghigh_ignores_kubelet_exporter(self):
        alert = {
            "name": "CPUThrottlingHigh",
            "service": "prometheus-kube-prometheus-kubelet",
            "labels": {
                "alertname": "CPUThrottlingHigh",
                "container": "scheduler",
                "pod": "shop-scheduler-db6855c5-qfjtd",
                "service": "prometheus-kube-prometheus-kubelet",
            },
        }
        service = extract_service_from_alert(alert)
        self.assertEqual(service, "scheduler")

    def test_05b_custom_valid_service_preserved(self):
        alert = {
            "name": "CustomAlert",
            "service": "artefacts",
            "labels": {
                "service": "artefacts",
            },
        }
        service = extract_service_from_alert(alert)
        self.assertEqual(service, "artefacts")

    def test_05b_exporter_suffix_excluded(self):
        alert = {
            "name": "PostgresDown",
            "service": "orders-postgres-exporter-prod-prometheus-postg",
            "labels": {
                "service": "orders-postgres-exporter-prod-prometheus-postg",
            },
        }
        service = extract_service_from_alert(alert)
        self.assertIsNone(service)

    def test_05c_assignee_not_overwritten_on_resolve(self):
        # Setup mock db and incident
        tenant_id = "test-tenant"
        incident_id = uuid4()
        incident = Incident(
            id=incident_id,
            tenant_id=tenant_id,
            status=IncidentStatus.FIRING.value,
            assignee=None,
            affected_services=[],
        )

        mock_session = MagicMock()
        bl = IncidentBl(tenant_id=tenant_id, session=mock_session)

        # Mock get_incident_by_id
        import keep.api.bl.incidents_bl as incidents_bl_mod

        original_get_incident = incidents_bl_mod.get_incident_by_id
        incidents_bl_mod.get_incident_by_id = MagicMock(return_value=incident)
        bl._IncidentBl__postprocess_incident_change = MagicMock(
            side_effect=lambda inc: inc
        )

        try:
            # 1. User Alice acknowledges the incident
            alice = AuthenticatedEntity(
                tenant_id=tenant_id,
                email="alice@example.com",
            )
            bl.change_status(incident_id, IncidentStatus.ACKNOWLEDGED, alice)
            self.assertEqual(incident.status, IncidentStatus.ACKNOWLEDGED.value)
            self.assertEqual(incident.assignee, "alice@example.com")

            # 2. Automated rule auto-resolves the incident with change_by="system"
            system_user = AuthenticatedEntity(
                tenant_id=tenant_id,
                email="system",
            )
            bl.change_status(incident_id, IncidentStatus.RESOLVED, system_user)
            self.assertEqual(incident.status, IncidentStatus.RESOLVED.value)
            # Assignee MUST remain alice, NOT overwritten by "system"!
            self.assertEqual(incident.assignee, "alice@example.com")

            # 3. User Bob resolves another incident
            incident.status = IncidentStatus.ACKNOWLEDGED.value
            bob = AuthenticatedEntity(
                tenant_id=tenant_id,
                email="bob@example.com",
            )
            bl.change_status(incident_id, IncidentStatus.RESOLVED, bob)
            self.assertEqual(incident.status, IncidentStatus.RESOLVED.value)
            # Assignee MUST STILL remain alice!
            self.assertEqual(incident.assignee, "alice@example.com")

        finally:
            incidents_bl_mod.get_incident_by_id = original_get_incident


if __name__ == "__main__":
    unittest.main()
