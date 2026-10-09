export type NormalizedFields = Partial<Record<
  "cluster" | "environment" | "namespace" | "kind" | "resource" | "workload" | "service",
  string | null
>>;

export interface EventPresentation {
  id: string;
  config_digest: string;
  title: string;
  description: string;
  fields: { path: string; label: string; value: string | number | boolean | null; known: boolean; source?: string | null }[];
  links: { label: string; url: string }[];
  missing_fields: string[];
  severity_color?: string | null;
}
