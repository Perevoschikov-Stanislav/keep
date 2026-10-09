import type { AlertCorrelation, IncidentCorrelation, IncidentLifecycle, IncidentAutomation } from "@/shared/lib/incident-correlation";
import type { EventPresentation } from "@/shared/lib/event-presentation";

const REASONS: Record<string, string> = {
  matched: "Matched the rule",
  separate_alert: "Incomplete data: separate alert",
  missing_required: "Required grouping fields are missing",
  overlap_lower_priority: "A rule with higher priority was selected",
  match_evaluation_error: "The rule could not be evaluated",
  no_matching_rule: "No matching rule",
  history_only: "Historical version: grouping unchanged",
  manually_unlinked: "The alert was manually removed",
  not_firing: "A resolved alert cannot start an incident",
  invalid_event_time: "Provider event time is missing or invalid",
};

export function CorrelationExplanation({ correlation, presentation, alertsCount }: {
  correlation?: IncidentCorrelation | null; presentation?: EventPresentation | null; alertsCount: number;
}) {
  if (!correlation) return null;
  const labels = Object.fromEntries((presentation?.fields ?? []).map((field) => [field.path, field.label]));
  return <section className="space-y-2 text-sm" aria-label="Incident grouping">
    <h3 className="font-medium">Grouping</h3>
    <p>Rule: {correlation.rule_id} · revision {correlation.rule_version.slice(0, 12)}</p>
    <p>{alertsCount} distinct alerts · threshold {correlation.policy.threshold} · window {correlation.policy.window_seconds}s</p>
    <p>{correlation.window_start} — {correlation.window_end} (UTC; upper bound excluded)</p>
    <p>Overlap: {correlation.overlap} · auto-resolve: {correlation.lifecycle.resolve_on}</p>
    {correlation.fallback && <p>Incomplete data: grouped by this alert&apos;s fingerprint.</p>}
    <dl>{correlation.group_values.map(([path, value]) => <div key={path} className="flex gap-2">
      <dt>{labels[path] ?? path}:</dt><dd>{JSON.stringify(value[1])} <span className="text-gray-500">({value[0]})</span></dd>
    </div>)}</dl>
    <details><summary>Match expression</summary><pre className="whitespace-pre-wrap break-words">{correlation.policy.match}</pre></details>
  </section>;
}

export function LifecycleExplanation({ lifecycle }: { lifecycle?: IncidentLifecycle | null }) {
  if (!lifecycle?.flapping) return null;
  const flapping = lifecycle.flapping;
  return <section className="space-y-2 text-sm" aria-label="Incident lifecycle">
    <h3 className="font-medium">Lifecycle {flapping.active && <span className="text-orange-700">· Flapping</span>}</h3>
    <p>Episode {lifecycle.episode} · started {lifecycle.episode_start} (UTC)</p>
    <p>Clock: {lifecycle.clock} · policy {lifecycle.policy_version?.slice(0, 12)}</p>
    {lifecycle.members && <p>{lifecycle.members.resolved}/{lifecycle.members.total} alerts resolved · resolution: {lifecycle.members.resolution}</p>}
    <p>{flapping.transition_count} firing/resolved transitions in {flapping.window_seconds}s · threshold {flapping.transition_threshold}</p>
    <p>{flapping.enabled ? `Flapping resets after ${flapping.reset_after_seconds}s without a transition.` : "Flapping classification is disabled."}</p>
    {flapping.since && <p>Flapping since {flapping.since} (UTC)</p>}
    {flapping.last_transition_at && <p>Last firing/resolved transition: {flapping.last_transition_at} (UTC)</p>}
    {lifecycle.reopened_at && <p>Reopened: {lifecycle.reopened_at} (UTC)</p>}
  </section>;
}

export function AutomationExplanation({ automation }: { automation?: IncidentAutomation | null }) {
  if (!automation) return null;
  return <section className="space-y-2 text-sm" aria-label="Incident automation">
    <h3 className="font-medium">SLA and escalation</h3>
    <p>ACK deadline: {automation.ack_deadline_at} (UTC) · {automation.ack_breached ? "Breached" : "Within deadline"}</p>
    <p>Level: {automation.level ?? "Not reached"} · episode {automation.episode}</p>
    <p>Policy: {automation.policy_id} · revision {automation.policy_version.slice(0, 12)}</p>
    {automation.next_due_at && <p>Next evaluation: {automation.next_due_at} (UTC)</p>}
    {automation.stopped_reason && <p>Stopped: {automation.stopped_reason}</p>}
    {automation.last_decision && <p>Last decision: {automation.last_decision.reason} · {automation.last_decision.at} (UTC)</p>}
    {automation.last_result && <><p>Last operation: {automation.last_result.status}{automation.last_result.reason && ` · ${automation.last_result.reason}`}</p>
      {Object.entries(automation.last_result.steps ?? {}).filter(([, step]) => step.skipped).map(([name, step]) =>
        <p key={name}>{name}: skipped · {step.reason}</p>)}
    </>}
  </section>;
}

export function AlertCorrelationExplanation({ correlation }: { correlation?: AlertCorrelation | null }) {
  if (!correlation) return null;
  return <section className="space-y-2 text-sm" aria-label="Alert grouping">
    <h3 className="font-medium">Grouping</h3>
    {correlation.decisions.map((decision, index) => <div key={index}>
      <p>{decision.rule_id && `${decision.rule_id}: `}{REASONS[decision.reason] ?? decision.reason}</p>
      {decision.incident_id && <p>Incident: {decision.incident_id}</p>}
      {decision.missing_fields?.map((field) => <p key={field.path}>{field.path}: {field.state}</p>)}
    </div>)}
  </section>;
}
