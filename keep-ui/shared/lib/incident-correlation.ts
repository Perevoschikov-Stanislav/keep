export type CorrelationValue = [string, unknown?];
export type GroupValues = [string, CorrelationValue][];
export interface IncidentCorrelation {
  rule_id: string;
  rule_version: string;
  config_digest: string;
  group_values: GroupValues;
  fallback: boolean;
  overlap: "first_match" | "parallel";
  window_start: string;
  window_end: string;
  policy: { window_seconds: number; threshold: number; match: string; create_on: "any" | "all" };
  lifecycle: { resolve_on: string };
}
export interface AlertCorrelation {
  decisions: {
    rule_id?: string;
    rule_version?: string;
    reason: string;
    incident_id?: string;
    group_values?: GroupValues;
    missing_fields?: { path: string; state: string }[];
  }[];
}

export interface IncidentLifecycle {
  revision: number;
  episode?: number;
  episode_start?: string;
  reopened_at?: string;
  resolved_at?: string;
  clock?: "event_time" | "receive_time";
  policy_version?: string;
  members?: { total: number; resolved: number; active: number; resolution: "full" | "partial" | "none" };
  flapping?: {
    enabled: boolean; active: boolean; transition_count: number; window_seconds: number;
    transition_threshold: number; reset_after_seconds: number; since: string | null; last_transition_at: string | null;
  };
}

export interface IncidentAutomation {
  episode: number; policy_id: string; policy_version: string; origin: string;
  ack_deadline_at: string; ack_breached: boolean; level: string | null; next_due_at: string | null;
  stopped_reason: string | null;
  last_decision?: { reason: string; at: string; level?: string | null };
  last_result?: { operation_id: string; status: string; at: string; reason?: string;
    steps?: Record<string, { skipped: boolean; reason: string | null }> };
}
