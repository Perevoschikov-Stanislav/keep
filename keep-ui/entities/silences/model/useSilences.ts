import useSWR, { useSWRConfig } from "swr";
import { useCallback, useRef } from "react";
import { v4 as uuidv4 } from "uuid";
import { toast } from "react-toastify";
import { useApi } from "@/shared/lib/hooks/useApi";
import { useHydratedSession } from "@/shared/lib/hooks/useHydratedSession";
import { showErrorToast } from "@/shared/ui";
import {
  CancelSilenceCommand,
  CreateSilenceCommand,
  EffectiveSilenceQuery,
  EffectiveSilenceResponse,
  Selector,
  SilenceChanges,
  SilenceDto,
  SilenceListResponse,
  SilenceMutationResponse,
  SilenceState,
  SilenceTarget,
  UpdateSilenceCommand,
} from "./types";

interface UseSilencesOptions {
  state?: SilenceState | "all";
  team_id?: string | null;
  limit?: number;
  cursor?: string | null;
}

export function useSilences(options: UseSilencesOptions = {}) {
  const api = useApi();
  const { data: session } = useHydratedSession();
  const { state, team_id, limit = 100, cursor } = options;

  const queryParams = new URLSearchParams();
  if (state && state !== "all") {
    queryParams.append("state", state);
  }
  if (team_id !== undefined) {
    queryParams.append("team_id", team_id ?? "");
  }
  if (limit) {
    queryParams.append("limit", limit.toString());
  }
  if (cursor) {
    queryParams.append("cursor", cursor);
  }

  const queryString = queryParams.toString();
  const url = api.isReady() ? `/silences${queryString ? `?${queryString}` : ""}` : null;

  const { data, error, isLoading, isValidating, mutate } = useSWR<SilenceListResponse>(
    url ? [url, session?.user?.email || "guest"] : null,
    async ([path]: [string, string]) => api.get(path),
    {
      revalidateOnFocus: true,
      dedupingInterval: 2000,
      refreshInterval: 30000,
    }
  );

  return {
    silences: data?.items ?? [],
    totalCount: data?.items?.length ?? 0,
    nextCursor: data?.next_cursor ?? null,
    evaluatedAt: data?.evaluated_at,
    error,
    isLoading,
    isValidating,
    mutate,
  };
}

export function useSilence(id: string | null) {
  const api = useApi();
  const { data: session } = useHydratedSession();
  const url = api.isReady() && id ? `/silences/${id}` : null;

  const { data, error, isLoading, mutate } = useSWR<SilenceDto>(
    url ? [url, session?.user?.email || "guest"] : null,
    async ([path]: [string, string]) => api.get(path)
  );

  return {
    silence: data,
    error,
    isLoading,
    mutate,
  };
}

export function useSilenceActions() {
  const api = useApi();
  const { data: session } = useHydratedSession();
  const { mutate: globalMutate } = useSWRConfig();
  const pendingRequests = useRef(new Map<string, string>());
  const requestId = (method: string, path: string, body: unknown) => {
    const key = JSON.stringify([session?.user?.email, method, path, body]);
    if (!pendingRequests.current.has(key)) pendingRequests.current.set(key, uuidv4());
    return { key, id: pendingRequests.current.get(key)! };
  };
  const detailOf = (error: any) => error?.responseJson?.detail ?? error?.response?.data?.detail;
  const releaseRejected = (key: string, error: any) => {
    const status = error?.statusCode ?? error?.response?.status;
    if (status >= 400 && status < 500) pendingRequests.current.delete(key);
  };
  const endpoint = (key: unknown): string | undefined => {
    const value = Array.isArray(key) ? key[0] : key;
    return typeof value === "string" ? value : undefined;
  };

  const revalidateAll = useCallback(async () => {
    await Promise.allSettled([
      globalMutate(
        (key) => endpoint(key)?.startsWith("/silences") ?? false,
        undefined,
        { revalidate: true }
      ),
      globalMutate(
        (key) => ["/alerts", "/incidents", "/preset"].some((prefix) => endpoint(key)?.startsWith(prefix)),
        undefined,
        { revalidate: true }
      ),
    ]);
  }, [globalMutate]);

  const createSilence = useCallback(
    async (params: {
      team_id?: string | null;
      selector: Selector;
      starts_at?: string | null;
      ends_at?: string | null;
      comment: string;
      correlation_id?: string | null;
    }): Promise<SilenceDto | null> => {
      const body = {
        schema_version: 1,
        team_id: params.team_id ?? null,
        selector: params.selector,
        starts_at: params.starts_at ?? null,
        ends_at: params.ends_at ?? null,
        comment: params.comment.trim(),
        correlation_id: params.correlation_id ?? null,
      };
      const request = requestId("POST", "/silences", body);
      const command: CreateSilenceCommand = { ...body, schema_version: 1, client_request_id: request.id };

      try {
        const response: SilenceMutationResponse = await api.post("/silences", command);
        pendingRequests.current.delete(request.key);
        toast.success(
          response.replayed
            ? "Silence already exists (idempotent request)"
            : "Silence rule created successfully"
        );
        await revalidateAll();
        return response.result;
      } catch (err: any) {
        releaseRejected(request.key, err);
        const detail = detailOf(err);
        if (detail?.message) {
          toast.error(`Failed to create silence: ${detail.message}`);
        } else {
          showErrorToast(err, "Failed to create silence");
        }
        throw err;
      }
    },
    [api, revalidateAll, session?.user?.email]
  );

  const updateSilence = useCallback(
    async (
      silenceId: string,
      expectedRevision: number,
      changes: SilenceChanges,
      correlation_id?: string | null
    ): Promise<SilenceDto | null> => {
      const body = {
        schema_version: 1,
        expected_revision: expectedRevision,
        changes,
        correlation_id: correlation_id ?? null,
      };
      const request = requestId("PATCH", silenceId, body);
      const command: UpdateSilenceCommand = { ...body, schema_version: 1, client_request_id: request.id };

      try {
        const response: SilenceMutationResponse = await api.patch(
          `/silences/${silenceId}`,
          command
        );
        pendingRequests.current.delete(request.key);
        toast.success("Silence rule updated successfully");
        await revalidateAll();
        return response.result;
      } catch (err: any) {
        releaseRejected(request.key, err);
        const detail = detailOf(err);
        if (detail?.code === "revision_conflict") {
          toast.error(
            `Revision conflict: Silence was modified by another operator (rev ${detail.current_revision}). Refreshing...`
          );
          await revalidateAll();
        } else if (detail?.message) {
          toast.error(`Failed to update silence: ${detail.message}`);
        } else {
          showErrorToast(err, "Failed to update silence");
        }
        throw err;
      }
    },
    [api, revalidateAll, session?.user?.email]
  );

  const cancelSilence = useCallback(
    async (
      silenceId: string,
      expectedRevision: number,
      reason: string = "Cancelled by user via UI",
      correlation_id?: string | null
    ): Promise<SilenceDto | null> => {
      const body = {
        schema_version: 1,
        expected_revision: expectedRevision,
        reason: reason.trim() || "Cancelled by user via UI",
        correlation_id: correlation_id ?? null,
      };
      const request = requestId("CANCEL", silenceId, body);
      const command: CancelSilenceCommand = { ...body, schema_version: 1, client_request_id: request.id };

      try {
        const response: SilenceMutationResponse = await api.post(
          `/silences/${silenceId}/cancel`,
          command
        );
        pendingRequests.current.delete(request.key);
        toast.success("Silence rule cancelled successfully");
        await revalidateAll();
        return response.result;
      } catch (err: any) {
        releaseRejected(request.key, err);
        const detail = detailOf(err);
        if (detail?.code === "revision_conflict") {
          toast.error(
            `Revision conflict: Silence was modified by another operator (rev ${detail.current_revision}). Refreshing...`
          );
          await revalidateAll();
        } else if (detail?.message) {
          toast.error(`Failed to cancel silence: ${detail.message}`);
        } else {
          showErrorToast(err, "Failed to cancel silence");
        }
        throw err;
      }
    },
    [api, revalidateAll, session?.user?.email]
  );

  const getEffective = useCallback(
    async (targets: SilenceTarget[]): Promise<EffectiveSilenceResponse> => {
      const query: EffectiveSilenceQuery = {
        schema_version: 1,
        targets,
      };
      return api.post("/silences/effective", query);
    },
    [api]
  );

  return {
    createSilence,
    updateSilence,
    cancelSilence,
    getEffective,
    revalidateAll,
  };
}
