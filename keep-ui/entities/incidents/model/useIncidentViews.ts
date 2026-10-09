import useSWR from "swr";
import { useApi } from "@/shared/lib/hooks/useApi";

export interface IncidentView {
  id: string;
  name: string;
  cel: string;
}

const defaultViews: IncidentView[] = [{ id: "all", name: "ALL", cel: "" }];

export function combineIncidentCel(...parts: (string | null | undefined)[]) {
  return parts.filter((part) => part?.trim()).map((part) => `(${part})`).join(" && ");
}

export function useIncidentViews() {
  const api = useApi();
  const result = useSWR<IncidentView[]>(
    api.isReady() ? "/incidents/views" : null,
    (url: string) => api.get(url)
  );
  return { ...result, views: result.data || defaultViews };
}
