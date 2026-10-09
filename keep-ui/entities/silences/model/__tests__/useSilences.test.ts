import React from "react";
import { renderHook, act, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import { useSilenceActions, useSilences } from "@/entities/silences/model";
import { KeepApiError } from "@/shared/api/KeepApiError";
import { toast } from "react-toastify";

let mockSessionEmail = "alice@example.test";
jest.mock("@/shared/lib/hooks/useHydratedSession", () => ({
  useHydratedSession: () => ({ data: { user: { email: mockSessionEmail } } }),
}));
jest.mock("react-toastify", () => ({ toast: { error: jest.fn(), success: jest.fn() } }));

const mockPost = jest.fn();
const mockPatch = jest.fn();
const mockGet = jest.fn();

jest.mock("@/shared/lib/hooks/useApi", () => ({
  useApi: () => ({
    isReady: () => true,
    post: mockPost,
    patch: mockPatch,
    get: mockGet,
  }),
}));

const mockMutate = jest.fn();
jest.mock("swr", () => {
  const original = jest.requireActual("swr");
  return {
    __esModule: true,
    ...original,
    useSWRConfig: () => ({
      mutate: mockMutate,
    }),
  };
});

describe("useSilenceActions", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockSessionEmail = "alice@example.test";
    mockMutate.mockResolvedValue(undefined);
  });

  it("createSilence issues POST /silences with v1 wire schema and generated client_request_id", async () => {
    mockPost.mockResolvedValue({
      schema_version: 1,
      client_request_id: "req-1",
      replayed: false,
      result: { id: "silence-123", revision: 1 },
    });

    const { result } = renderHook(() => useSilenceActions());

    let created: any;
    await act(async () => {
      created = await result.current.createSilence({
        team_id: "team-ops",
        selector: { kind: "alert", fingerprints: ["fp-test"] },
        starts_at: "2026-10-04T12:00:00Z",
        ends_at: null,
        comment: "Indefinite silence for maintenance",
      });
    });

    expect(mockPost).toHaveBeenCalledWith(
      "/silences",
      expect.objectContaining({
        schema_version: 1,
        team_id: "team-ops",
        selector: { kind: "alert", fingerprints: ["fp-test"] },
        starts_at: "2026-10-04T12:00:00Z",
        ends_at: null,
        comment: "Indefinite silence for maintenance",
        client_request_id: expect.any(String),
      })
    );
    expect(created.id).toBe("silence-123");
    expect(mockMutate).toHaveBeenCalled();
  });

  it("updateSilence issues PATCH /silences/{id} with expected_revision", async () => {
    mockPatch.mockResolvedValue({
      schema_version: 1,
      client_request_id: "req-2",
      replayed: false,
      result: { id: "silence-123", revision: 2 },
    });

    const { result } = renderHook(() => useSilenceActions());

    let updated: any;
    await act(async () => {
      updated = await result.current.updateSilence("silence-123", 1, {
        comment: "Updated reason",
        ends_at: "2026-10-05T12:00:00Z",
      });
    });

    expect(mockPatch).toHaveBeenCalledWith(
      "/silences/silence-123",
      expect.objectContaining({
        schema_version: 1,
        expected_revision: 1,
        changes: {
          comment: "Updated reason",
          ends_at: "2026-10-05T12:00:00Z",
        },
      })
    );
    expect(updated.revision).toBe(2);
  });

  it("cancelSilence issues POST /silences/{id}/cancel with expected_revision and reason", async () => {
    mockPost.mockResolvedValue({
      schema_version: 1,
      client_request_id: "req-3",
      replayed: false,
      result: { id: "silence-123", revision: 3, state: "cancelled" },
    });

    const { result } = renderHook(() => useSilenceActions());

    let cancelled: any;
    await act(async () => {
      cancelled = await result.current.cancelSilence(
        "silence-123",
        2,
        "Issue resolved"
      );
    });

    expect(mockPost).toHaveBeenCalledWith(
      "/silences/silence-123/cancel",
      expect.objectContaining({
        schema_version: 1,
        expected_revision: 2,
        reason: "Issue resolved",
      })
    );
    expect(cancelled.state).toBe("cancelled");
  });

  it("keeps the command ID when retrying an uncertain create", async () => {
    mockPost.mockRejectedValueOnce(new Error("connection lost"));
    mockPost.mockResolvedValueOnce({ result: { id: "accepted" }, replayed: true });
    const { result } = renderHook(() => useSilenceActions());
    const params = { team_id: "alpha", selector: { kind: "alert" as const, fingerprints: ["a"] }, comment: "test" };
    await act(async () => {
      await expect(result.current.createSilence(params)).rejects.toThrow("connection lost");
      await result.current.createSilence(params);
    });
    expect(mockPost.mock.calls[0][1]).toEqual(mockPost.mock.calls[1][1]);
  });

  it("handles the real KeepApiError revision conflict and refreshes affected caches", async () => {
    const error = new KeepApiError("conflict", "/silences/id", "", {
      detail: { code: "revision_conflict", current_revision: 2, message: "Revision changed" },
    }, 409);
    mockPatch.mockRejectedValueOnce(error);
    const { result } = renderHook(() => useSilenceActions());
    await act(async () => {
      await expect(result.current.updateSilence("id", 1, { comment: "changed" })).rejects.toBe(error);
    });
    expect(toast.error).toHaveBeenCalledWith(expect.stringContaining("Revision conflict"));
    expect(mockMutate.mock.calls.some(([predicate]) => predicate(["/silences?limit=100", "alice@example.test"]))).toBe(true);
    expect(mockMutate.mock.calls.some(([predicate]) => predicate(["/incidents/id", "alice@example.test"]))).toBe(true);
  });

  it("does not report a committed create as failed when refreshing a cache fails", async () => {
    mockPost.mockResolvedValue({ result: { id: "saved" }, replayed: false });
    mockMutate.mockRejectedValue(new Error("refresh failed"));
    const { result } = renderHook(() => useSilenceActions());
    await act(async () => {
      await expect(result.current.createSilence({ selector: { kind: "alert", fingerprints: ["a"] }, comment: "test" }))
        .resolves.toEqual({ id: "saved" });
    });
  });
});

function wrapperForCache() {
  const cache = new Map();
  return ({ children }: { children: React.ReactNode }) =>
    React.createElement(SWRConfig, { value: { provider: () => cache } }, children);
}

describe("silence reads", () => {
  beforeEach(() => { jest.clearAllMocks(); mockSessionEmail = "alice@example.test"; });

  it("distinguishes all teams from explicitly unassigned objects", async () => {
    mockGet.mockResolvedValue({ items: [], next_cursor: null });
    const { rerender } = renderHook(({ team }) => useSilences({ team_id: team }), {
      initialProps: { team: undefined as string | null | undefined }, wrapper: wrapperForCache(),
    });
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith("/silences?limit=100"));
    rerender({ team: null });
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith("/silences?team_id=&limit=100"));
  });

  it("never returns the previous user's cached rules while the next user loads", async () => {
    mockGet.mockResolvedValueOnce({ items: [{ id: "alice-only" }], next_cursor: null });
    const { result, rerender } = renderHook(() => useSilences(), { wrapper: wrapperForCache() });
    await waitFor(() => expect(result.current.silences).toHaveLength(1));
    mockGet.mockImplementationOnce(() => new Promise(() => {}));
    mockSessionEmail = "bob@example.test";
    rerender();
    expect(result.current.silences).toEqual([]);
  });
});
