import useSWR from "swr";
import { useApi } from "./useApi";
import { useConfig } from "@/utils/hooks/useConfig";
import { useHydratedSession } from "./useHydratedSession";

export interface UserPermissions {
  role: string;
  scopes: string[];
  writable_teams: string[] | null;
}

export function allowsAction(
  permissions: UserPermissions | undefined,
  scope: string,
  entity?: { team_id?: string | null }
) {
  if (!permissions) return false;
  const action = scope.split(":")[0];
  if (!permissions.scopes.some((value) => value === scope || value === `${action}:*`)) {
    return false;
  }
  return !entity || permissions.writable_teams === null ||
    (!!entity.team_id && permissions.writable_teams.includes(entity.team_id));
}

export function useUserPermissions() {
  const api = useApi();
  const { data: session } = useHydratedSession();
  const { data: config } = useConfig();
  const { data, isLoading } = useSWR<UserPermissions>(
    api.isReady() ? ["/auth/users/me/permissions", session?.user?.email || "guest"] : null,
    ([url]: [string, string]) => api.get(url)
  );
  return {
    permissions: data,
    isLoading,
    can: (scope: string, entity?: { team_id?: string | null }) =>
      !config?.READ_ONLY && allowsAction(data, scope, entity),
  };
}
