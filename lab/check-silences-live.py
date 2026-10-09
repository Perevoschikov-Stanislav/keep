#!/usr/bin/env python3
"""Task 22 real HTTP and processing checks against the isolated k3d lab."""

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = "keep-silences-22"


def kubectl(*args, code=None):
    command = ["kubectl", "--context", "k3d-local", "--cache-dir", str(ROOT / ".lab-work/kube-cache"),
               "-n", NAMESPACE, *args]
    return subprocess.check_output(command, input=code.encode() if code else None).decode()


def in_backend(code):
    result = kubectl("exec", "-i", "deployment/backend", "--", "python", "-", code=code)
    return json.loads(result.split("TASK22_RESULT ")[-1])


def stamp(value):
    return value.isoformat().replace("+00:00", "Z")


class Checks:
    def __init__(self, args):
        self.args = args
        self.results = []

    def check(self, label, condition):
        self.results.append({"check": label, "passed": bool(condition)})
        if not condition:
            raise AssertionError(label)
        print("PASS", label, flush=True)

    def http(self, path, method="GET", body=None, actor="admin", expected=200, headers=None):
        request = Request(self.args.url + "/v2" + path,
            data=json.dumps(body).encode() if body is not None else None, method=method,
            headers={"Content-Type": "application/json", "Cookie": "silence_lab_actor=" + actor,
                     **(headers or {})})
        try:
            response = urlopen(request, timeout=20)
        except HTTPError as error:
            response = error
        raw = response.read()
        data = json.loads(raw) if raw else None
        if response.status != expected:
            raise AssertionError(f"{method} {path}: expected {expected}, got {response.status}: {data}")
        return data

    def wait(self, label, callback, timeout=25):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = callback()
            if result:
                self.check(label, True)
                return result
            time.sleep(0.25)
        self.check(label, False)

    def create(self, selector, team="cedar", actor="admin", **fields):
        body = {"schema_version": 1, "client_request_id": str(uuid4()), "team_id": team,
                "selector": selector, "starts_at": None, "ends_at": None,
                "comment": "Task22 HTTP verification", "correlation_id": None, **fields}
        return self.http("/silences", "POST", body, actor=actor, expected=201)["result"]

    def cancel(self, rule, actor="admin"):
        return self.http("/silences/" + rule["id"] + "/cancel", "POST", {
            "schema_version": 1, "client_request_id": str(uuid4()),
            "expected_revision": rule["revision"], "reason": "Task22 cancellation",
            "correlation_id": None}, actor=actor)["result"]

    def alert(self, fingerprint, team="cedar", sequence=1):
        self.http("/alerts/event", "POST", {"fingerprint": fingerprint, "name": "Task22Cedar",
            "status": "firing", "severity": "critical", "source": ["task22"],
            "zone": "zone-" + team, "description": "Sequence " + str(sequence),
            "labels": {"sequence": str(sequence)}}, expected=202)
        return self.wait("ingested " + fingerprint + " seq=" + str(sequence), lambda: next(
            (a for a in self.http("/alerts") if a["fingerprint"] == fingerprint
             and a.get("description") == "Sequence " + str(sequence)), None))

    def records(self):
        with urlopen(self.args.receiver + "/records", timeout=5) as response:
            return json.load(response)

    def api(self):
        for fingerprint, team in [("task22-cedar-a", "cedar"), ("task22-cedar-b", "cedar"),
                                  ("task22-quartz-a", "quartz"), ("task22-legacy", "cedar")]:
            self.alert(fingerprint, team)
        self.check("real ingest assigns team from IaC zone",
            {a["team_id"] for a in self.http("/alerts", actor="cedar")} == {"cedar"})
        selector = {"kind": "alert", "fingerprints": ["task22-cedar-a"]}
        own = self.create(selector, actor="cedar", comment="Task22 own team")
        other = self.create({"kind": "alert", "fingerprints": ["task22-quartz-a"]},
                            team="quartz", comment="Task22 foreign team")
        self.check("responder can create own-team rule", own["state"] == "active")
        self.check("viewer reads rule", self.http("/silences/" + own["id"], actor="viewer")["id"] == own["id"])
        self.check("shared visibility reads Cedar from Quartz",
            self.http("/silences/" + own["id"], actor="quartz")["id"] == own["id"])
        self.http("/silences/" + other["id"], actor="cedar", expected=404)
        self.check("foreign direct ID hidden", True)
        denied = {"schema_version": 1, "client_request_id": str(uuid4()), "team_id": "cedar",
            "selector": selector, "starts_at": None, "ends_at": None,
            "comment": "Denied", "correlation_id": None}
        self.http("/silences", "POST", denied, actor="viewer", expected=403)
        self.check("viewer mutation denied by actual API", True)
        self.http("/silences/" + own["id"] + "/cancel", "POST", {
            "schema_version": 1, "client_request_id": str(uuid4()), "expected_revision": own["revision"],
            "reason": "Denied shared visibility", "correlation_id": None}, actor="quartz", expected=403)
        self.check("shared visibility grants no mutation", True)
        self.http("/silences", headers={"X-KEEP-USER": "admin@example.test"}, expected=403)
        self.check("unverified legacy actor rejected", True)
        count = len(self.http("/silences")["items"])
        denied["client_request_id"] = str(uuid4())
        denied["selector"]["fingerprints"].append("task22-quartz-a")
        self.http("/silences", "POST", denied, actor="cedar", expected=404)
        self.check("mixed-target command atomic", len(self.http("/silences")["items"]) == count)
        now = datetime.now(timezone.utc)
        scheduled = self.create({"kind": "filter", "cel": "true"},
            starts_at=stamp(now + timedelta(seconds=5)), ends_at=stamp(now + timedelta(seconds=11)))
        self.check("future rule is scheduled", scheduled["state"] == "scheduled")
        self.wait("scheduled rule activated by production lifespan worker",
            lambda: self.http("/silences/" + scheduled["id"])["state"] == "active")
        self.wait("scheduled rule expired by production lifespan worker",
            lambda: self.http("/silences/" + scheduled["id"])["state"] == "expired")
        self.wait("outbox lifecycle arrived at real HTTP receiver",
            lambda: {r["body"]["event_type"] for r in self.records()
                if r["body"].get("silence_id") == scheduled["id"]} >=
                {"silence.created", "silence.activated", "silence.expired"})
        overlap = self.create({"kind": "alert", "fingerprints": ["task22-cedar-a"]})
        self.cancel(own, actor="cedar")
        alert = next(a for a in self.http("/alerts", actor="cedar") if a["fingerprint"] == "task22-cedar-a")
        self.check("cancel one overlapping rule retains effective silence", alert["silence"]["silenced"])
        self.cancel(overlap)
        self.cancel(other)
        persistent = self.create({"kind": "filter", "cel": "name == 'Task22Persistent'"},
                                 comment="Task22 persistent across restart")
        (self.args.output_dir / "persistent.json").write_text(json.dumps(persistent))
        self.check("indefinite rule persisted", persistent["ends_at"] is None)

    def processing(self):
        for old in self.http("/silences")["items"]:
            if old["state"] == "active" and old["comment"] == "Task22 HTTP verification" and old["selector"] == {"kind": "alert", "fingerprints": ["task22-processing"]}:
                self.cancel(old)
        setup = in_backend('''
import json
from keep.workflowmanager.workflowstore import WorkflowStore
from keep.api.core.db import engine
from keep.api.models.db.rule import Rule
from sqlmodel import Session, select
from datetime import datetime
workflow = {"id": "task22-dispatch", "name": "task22-dispatch", "disabled": False,
    "triggers": [{"type": "alert", "filters": [{"key": "source", "value": "task22"}]}],
    "actions": [{"name": name, "notification": notification,
        "provider": {"type": "http", "with": {"method": "POST", "url": "http://receiver:8090/" + name,
            "body": {"fingerprint": "{{ alert.fingerprint }}"}}}}
        for name, notification in [("automation", False), ("notification", True)]]}
row = WorkflowStore().create_workflow("keep", "task22-lab", workflow)
with Session(engine) as session:
    if not session.exec(select(Rule).where(Rule.name == "task22-correlation")).first():
        session.add(Rule(tenant_id="keep", name="task22-correlation", definition={"sql":"1=1", "params":{}},
            definition_cel="name == 'Task22Cedar'", timeframe=3600, created_by="task22-lab",
            creation_time=datetime.utcnow(), grouping_criteria=["zone"], resolve_on="never"))
        session.commit()
print("TASK22_RESULT " + json.dumps({"workflow_id": row.id}))
''')
        self.check("classified notification and automation workflow configured", bool(setup["workflow_id"]))
        baseline = len([r for r in self.records() if r["path"] == "/notification"])
        self.alert("task22-processing", sequence=1)
        self.wait("unmuted notification sent", lambda: len(
            [r for r in self.records() if r["path"] == "/notification"]) > baseline, timeout=35)
        rule = self.create({"kind": "alert", "fingerprints": ["task22-processing"]})
        before = self.records()
        self.alert("task22-processing", sequence=2)
        self.wait("silenced non-notification automation executes", lambda: len(
            [r for r in self.records() if r["path"] == "/automation"]) > len(
            [r for r in before if r["path"] == "/automation"]), timeout=35)
        time.sleep(1)
        self.check("actual outgoing notification absent while silenced", len(
            [r for r in self.records() if r["path"] == "/notification"]) == len(
            [r for r in before if r["path"] == "/notification"]))
        result = in_backend('''
import json
from sqlalchemy import text
from keep.api.core.db import engine
with engine.connect() as connection:
    events = connection.execute(text("SELECT event FROM alert WHERE fingerprint='task22-processing' ORDER BY timestamp")).scalars().all()
    linked = connection.execute(text("SELECT count(*) FROM lastalerttoincident WHERE fingerprint='task22-processing'")).scalar()
    skips = connection.execute(text("SELECT count(*) FROM workflowexecutionlog WHERE message LIKE 'Action notification skipped: silenced,%' AND workflow_execution_id IN (SELECT id FROM workflowexecution WHERE workflow_id=:id)"), {"id": ''' + repr(setup["workflow_id"]) + '''}).scalar()
print("TASK22_RESULT " + json.dumps({"events": events, "linked": linked, "skips": skips}))
''')
        self.check("repeat event stored with firing status and original data",
            len(result["events"]) >= 2 and all(e["status"] == "firing" for e in result["events"]))
        self.check("correlation continues while silenced", result["linked"] > 0)
        self.check("execution log records silence skip diagnostic", result["skips"] > 0)
        self.cancel(rule)
        before = len([r for r in self.records() if r["path"] == "/notification"])
        self.alert("task22-processing", sequence=3)
        self.wait("notifications resume after cancellation", lambda: len(
            [r for r in self.records() if r["path"] == "/notification"]) > before, timeout=35)

    def restart(self):
        expected = json.loads((self.args.output_dir / "persistent.json").read_text())
        actual = self.http("/silences/" + expected["id"])
        self.check("rule and revision survive backend and PostgreSQL restarts",
            actual["id"] == expected["id"] and actual["revision"] == expected["revision"] and actual["state"] == "active")

    def legacy(self):
        seed = in_backend('''
import json
from datetime import datetime, timedelta, timezone
from keep.api.core.db import engine
from keep.api.models.db.alert import AlertEnrichment
from keep.api.models.db.maintenance_window import MaintenanceWindowRule
from sqlmodel import Session, select
with Session(engine) as session:
    if not session.exec(select(AlertEnrichment).where(AlertEnrichment.alert_fingerprint == "task22-legacy")).first():
        session.add(AlertEnrichment(tenant_id="keep", alert_fingerprint="task22-legacy", enrichments={
            "dismissed": 1, "dismissUntil": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            "dismissComment": "Task22 legacy reason", "dismissed_by": "legacy@example.test"}))
    if not session.exec(select(AlertEnrichment).where(AlertEnrichment.alert_fingerprint == "task22-cedar-b")).first():
        session.add(AlertEnrichment(tenant_id="keep", alert_fingerprint="task22-cedar-b", enrichments={"status":"suppressed"}))
    rule = session.exec(select(MaintenanceWindowRule).where(MaintenanceWindowRule.name == "Task22 old Maintenance")).first()
    if not rule:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        rule = MaintenanceWindowRule(tenant_id="keep", name="Task22 old Maintenance", created_by="lab",
            cel_query="name == 'Task22LegacyMaint'", start_time=now-timedelta(minutes=10),
            end_time=now+timedelta(hours=2), enabled=True, ignore_statuses=[])
        session.add(rule)
    session.commit()
    session.refresh(rule)
    print("TASK22_RESULT " + json.dumps({"maintenance_id": rule.id}))
''')
        def state():
            return in_backend('''
import json
from sqlalchemy import text
from keep.api.core.db import engine
with engine.connect() as c:
    data={"alerts":[[str(row[0]),row[1]] for row in c.execute(text("SELECT id, event FROM alert ORDER BY id"))],
          "enrichments":[list(row) for row in c.execute(text("SELECT alert_fingerprint,enrichments FROM alertenrichment ORDER BY alert_fingerprint"))],
          "rules":c.execute(text("SELECT count(*) FROM silence")).scalar()}
print("TASK22_RESULT " + json.dumps(data))
''')
        before = state()
        backup = self.args.output_dir / "pre-cutover.dump"
        with backup.open("wb") as stream:
            subprocess.run(["kubectl", "--context", "k3d-local", "--cache-dir", str(ROOT / ".lab-work/kube-cache"),
                "-n", NAMESPACE, "exec", "deployment/postgres", "--", "pg_dump", "-h", "/work",
                "-U", "keep", "-d", "keep", "-Fc"], stdout=stream, check=True)
        backup.chmod(0o600)
        cli = (ROOT / "lab/migrate-legacy-silences.py").read_text()
        output = kubectl("exec", "-i", "deployment/backend", "--", "python", "-", "--dry-run",
            "--tenant-id", "keep", "--report", "/work/task22-inventory.json", code=cli)
        (self.args.output_dir / "legacy-dry-run.log").write_text(output)
        inventory = json.loads(kubectl("exec", "deployment/backend", "--", "cat", "/work/task22-inventory.json"))
        (self.args.output_dir / "legacy-inventory.json").write_text(json.dumps(inventory, indent=2))
        self.check("real CLI dry-run changes no alerts, enrichments or silence rows", state() == before)
        self.check("unproven suppressed status explicitly requires review", any(
            a["type"] == "dismiss_status_provenance_required" for a in inventory["inventory"]["admin_action_required"]))
        plan = {"keep": {str(seed["maintenance_id"]): {"teams": ["cedar"], "cel": "name == 'Task22LegacyMaint'"}}}
        (self.args.output_dir / "maintenance-plan.json").write_text(json.dumps(plan, indent=2))
        in_backend("from pathlib import Path\nimport json\nPath('/work/task22-plan.json').write_text(" +
            repr(json.dumps(plan)) + ")\nprint('TASK22_RESULT {}')")
        for attempt in (1, 2):
            reviewed_path = "/work/task22-inventory.json"
            if attempt == 2:
                reviewed_path = "/work/task22-inventory-2.json"
                kubectl("exec", "-i", "deployment/backend", "--", "python", "-", "--dry-run",
                    "--tenant-id", "keep", "--report", reviewed_path, code=cli)
                (self.args.output_dir / "legacy-inventory-2.json").write_text(kubectl(
                    "exec", "deployment/backend", "--", "cat", reviewed_path))
            output = kubectl("exec", "-i", "deployment/backend", "--", "python", "-", "--apply",
                "--tenant-id", "keep", "--reviewed-inventory", reviewed_path,
                "--maintenance-team-plan", "/work/task22-plan.json", "--report", f"/work/task22-apply-{attempt}.json", code=cli)
            (self.args.output_dir / f"legacy-apply-{attempt}.log").write_text(output)
            report = json.loads(kubectl("exec", "deployment/backend", "--", "cat", f"/work/task22-apply-{attempt}.json"))
            (self.args.output_dir / f"legacy-apply-{attempt}.json").write_text(json.dumps(report, indent=2))
            if attempt == 1:
                self.check("reviewed CLI import creates dismissal and team Maintenance rules",
                    report["apply_result"]["dismissals_migrated"] == 1 and report["apply_result"]["maintenance_rules_migrated"] == 1)
            else:
                self.check("second CLI import is idempotent", report["apply_result"]["created_silence_ids"] == [])
        after = state()
        self.check("legacy import preserves all original event JSON and history", before["alerts"] == after["alerts"])
        enrichments = dict(after["enrichments"])
        self.check("import clears raw dismissed flag", not enrichments.get("task22-legacy", {}).get("dismissed"))
        self.check("unknown status override retained for administrator", enrichments["task22-cedar-b"]["status"] == "suppressed")

    def cutover(self):
        response = self.http("/maintenance", "POST", {"name": "Task22 denied old write", "cel_query": "true",
            "start_time": stamp(datetime.now(timezone.utc)), "duration_seconds": 3600}, expected=409)
        self.check("old Maintenance writer disabled after cutover",
                   response["detail"]["code"] == "legacy_maintenance_disabled")
        self.http("/alerts/event", "POST", {"fingerprint":"task22-maintenance-arrival", "name":"Task22LegacyMaint",
            "status":"firing", "severity":"critical", "source":["task22-cutover"], "zone":"zone-cedar"}, expected=202)
        alert = self.wait("old Maintenance no longer drops incoming matching alert", lambda: next(
            (a for a in self.http("/alerts") if a["fingerprint"] == "task22-maintenance-arrival"), None))
        self.check("imported team rule derives silence without replacing original status", alert["status"] == "firing" and alert["silence"]["silenced"])
        self.restart()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8012")
    parser.add_argument("--receiver", default="http://localhost:8013")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--phase", choices=["api", "processing", "restart", "legacy", "cutover"], required=True)
    args = parser.parse_args()
    args.output_dir = args.output_dir.resolve()
    if not args.output_dir.is_relative_to(ROOT / ".lab-work") or any(
        urlsplit(url).hostname not in {"localhost", "127.0.0.1"} for url in (args.url, args.receiver)):
        parser.error("Only the isolated localhost lab and fork .lab-work artifacts are supported")
    checks = Checks(args)
    try:
        getattr(checks, args.phase)()
    finally:
        (args.output_dir / (args.phase + "-result.json")).write_text(json.dumps(checks.results, indent=2))
    print(f"{args.phase}: {len(checks.results)} checks passed")


if __name__ == "__main__":
    main()
