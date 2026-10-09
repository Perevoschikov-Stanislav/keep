export type SilenceState = "scheduled" | "active" | "expired" | "cancelled";

export interface AlertSelector {
  kind: "alert";
  fingerprints: string[];
}

export interface IncidentSelector {
  kind: "incident";
  incident_ids: string[];
}

export interface FilterSelector {
  kind: "filter";
  cel: string;
}

export type Selector = AlertSelector | IncidentSelector | FilterSelector;

export interface SilenceActor {
  kind: "user" | "service";
  subject: string;
  issuer: string | null;
  display_name: string;
}

export interface SilenceDto {
  schema_version: 1;
  id: string;
  revision: number;
  tenant_id: string;
  team_id: string | null;
  selector: Selector;
  starts_at: string;
  ends_at: string | null;
  comment: string;
  created_by: SilenceActor;
  updated_by: SilenceActor;
  created_at: string;
  updated_at: string;
  cancelled_at: string | null;
  origin: string;
  correlation_id: string | null;
  state: SilenceState;
  evaluated_at: string;
  read_only?: boolean;
  synchronization?: { source_id: string; state: string; reason: string | null }[];
}

export interface SilenceReason {
  silence_id: string;
  revision: number;
  via: "fingerprint" | "filter" | "incident";
  incident_id: string | null;
  ends_at: string | null;
  read_only?: boolean;
}

export interface SilenceMetadata {
  evaluated_at: string;
  silenced: boolean;
  coverage: "none" | "partial" | "full";
  silenced_until: string | null;
  reasons: SilenceReason[];
  total_alerts: number;
  silenced_alerts: number;
  [key: string]: any;
}

export interface CreateSilenceCommand {
  schema_version: 1;
  client_request_id: string;
  team_id: string | null;
  selector: Selector;
  starts_at: string | null;
  ends_at: string | null;
  comment: string;
  correlation_id: string | null;
}

export interface SilenceChanges {
  selector?: Selector;
  starts_at?: string;
  ends_at?: string | null;
  comment?: string;
}

export interface UpdateSilenceCommand {
  schema_version: 1;
  client_request_id: string;
  expected_revision: number;
  changes: SilenceChanges;
  correlation_id: string | null;
}

export interface CancelSilenceCommand {
  schema_version: 1;
  client_request_id: string;
  expected_revision: number;
  reason: string;
  correlation_id: string | null;
}

export interface SilenceMutationResponse {
  schema_version: 1;
  client_request_id: string;
  replayed: boolean;
  result: SilenceDto;
}

export interface SilenceListResponse {
  schema_version: 1;
  evaluated_at: string;
  items: SilenceDto[];
  next_cursor: string | null;
}

export type AlertTarget = {
  kind: "alert";
  fingerprint: string;
};

export type IncidentTarget = {
  kind: "incident";
  incident_id: string;
};

export type SilenceTarget = AlertTarget | IncidentTarget;

export interface EffectiveSilenceQuery {
  schema_version: 1;
  targets: SilenceTarget[];
}

export interface EffectiveSilenceItem {
  target: SilenceTarget;
  silenced: boolean;
  coverage: "none" | "partial" | "full";
  silenced_until: string | null;
  reasons: SilenceReason[];
  total_alerts: number;
  silenced_alerts: number;
}

export interface EffectiveSilenceResponse {
  schema_version: 1;
  evaluated_at: string;
  items: EffectiveSilenceItem[];
}
