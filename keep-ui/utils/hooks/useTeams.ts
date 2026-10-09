import useSWR from "swr";
import { useApi } from "@/shared/lib/hooks/useApi";
import { useHydratedSession } from "@/shared/lib/hooks/useHydratedSession";

export interface ConfiguredTeam {
  id: string;
  groups: string[];
  zones: string[];
  visible_to: string[];
}

export interface TeamsResponse {
  enabled: boolean;
  visibility: "team" | "all" | null;
  teams: ConfiguredTeam[];
  configuration?: {
    digest: string;
    generation: number;
    revision: string;
    source?: string;
    applied_by?: string;
    applied_at?: string;
    result?: string;
    drift_count?: number | null;
  };
}

export function useTeams() {
  const api = useApi();
  const { data: session } = useHydratedSession();

  return useSWR<TeamsResponse>(
    api.isReady() ? ["/auth/teams", session?.user?.email || "guest"] : null,
    ([url]: [string, string]) => api.get(url)
  );
}
