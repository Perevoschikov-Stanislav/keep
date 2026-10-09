import { render, screen } from "@testing-library/react";
import { CorrelationExplanation, AlertCorrelationExplanation, LifecycleExplanation, AutomationExplanation } from "../CorrelationExplanation";
import type { IncidentCorrelation } from "@/shared/lib/incident-correlation";

const correlation: IncidentCorrelation = {
  rule_id: "workload", rule_version: "0123456789abcdef", config_digest: "digest",
  group_values: [["normalized.workload", ["string", "<script>alert(1)</script>"]]],
  fallback: false, overlap: "first_match", window_start: "2026-10-05T12:00:00",
  window_end: "2026-10-05T12:01:00", policy: { window_seconds: 60, threshold: 2, match: "normalized.kind == 'workload'", create_on: "any" },
  lifecycle: { resolve_on: "all_resolved" },
};

it("shows durable SLA state and notification skips without claiming delivery", () => {
  render(<AutomationExplanation automation={{ episode: 1, policy_id: "sla", policy_version: "0123456789abcdef",
    origin: "2026-10-05T12:00:00", ack_deadline_at: "2026-10-05T12:00:10", ack_breached: true, level: "second",
    next_due_at: null, stopped_reason: null, last_result: { operation_id: "operation", status: "success", at: "now",
      steps: { notify: { skipped: true, reason: "silenced" } } } }} />);
  expect(screen.getByRole("region", { name: "Incident automation" })).toHaveTextContent("Breached");
  expect(screen.getByText(/Level: second/)).toBeInTheDocument();
  expect(screen.getByText(/notify: skipped/)).toHaveTextContent("silenced");
});

it("explains the pinned rule, window, threshold and typed grouping fields as text", () => {
  const { container } = render(<CorrelationExplanation correlation={correlation} alertsCount={3} presentation={{
    id: "workload", config_digest: "digest", title: "Workload", description: "", missing_fields: [], links: [],
    fields: [{ path: "normalized.workload", label: "Deployment", value: "catalog", known: true, source: "labels.workload" }],
  }} />);
  expect(screen.getByText(/revision 0123456789ab/)).toBeInTheDocument();
  expect(screen.getByText(/3 distinct alerts/)).toHaveTextContent("threshold 2");
  expect(screen.getByText(/upper bound excluded/)).toBeInTheDocument();
  expect(screen.getByText("Deployment:")).toBeInTheDocument();
  expect(container.querySelector("script")).toBeNull();
  expect(screen.getByText(/<script>alert/)).toBeInTheDocument();
});

it("shows missing diagnostics and a separate-alert fallback", () => {
  render(<><CorrelationExplanation correlation={{ ...correlation, fallback: true }} alertsCount={1} />
    <AlertCorrelationExplanation correlation={{ decisions: [{ rule_id: "workload", reason: "missing_required",
      missing_fields: [{ path: "normalized.workload", state: "unknown_normalized" }] }] }} /></>);
  expect(screen.getByText(/this alert's fingerprint/)).toBeInTheDocument();
  expect(screen.getByText(/Required grouping fields are missing/)).toBeInTheDocument();
  expect(screen.getByText(/normalized.workload: unknown_normalized/)).toBeInTheDocument();
});

it("keeps legacy incidents and alerts without evidence unchanged", () => {
  const { container } = render(<><CorrelationExplanation alertsCount={2} /><AlertCorrelationExplanation /></>);
  expect(container).toBeEmptyDOMElement();
});

it("shows flapping, partial resolution and the new episode clock", () => {
  render(<LifecycleExplanation lifecycle={{ revision: 3, episode: 2, episode_start: "2026-10-05T12:00:11",
    clock: "event_time", policy_version: "0123456789abcdef", members: { total: 2, resolved: 1, active: 1, resolution: "partial" },
    flapping: { enabled: true, active: true, transition_count: 2, transition_threshold: 2, window_seconds: 60,
      reset_after_seconds: 30, since: "2026-10-05T12:00:11", last_transition_at: "2026-10-05T12:00:11" } }} />);
  expect(screen.getByRole("region", { name: "Incident lifecycle" })).toHaveTextContent("Flapping");
  expect(screen.getByText(/1\/2 alerts resolved/)).toHaveTextContent("partial");
  expect(screen.getByText(/Episode 2/)).toBeInTheDocument();
  expect(screen.getByText(/Clock: event_time/)).toBeInTheDocument();
  expect(screen.getByText(/2 firing\/resolved transitions in 60s/)).toBeInTheDocument();
  expect(screen.getByText(/resets after 30s/)).toBeInTheDocument();
});
