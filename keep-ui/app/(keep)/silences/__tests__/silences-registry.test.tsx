import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import Silences from "../silences";

const mockUseSilences = jest.fn();
const mockUpdate = jest.fn();
const mockMutate = jest.fn();
jest.mock("@/entities/silences/model", () => ({
  useSilences: (filters: unknown) => mockUseSilences(filters),
  useSilenceActions: () => ({ updateSilence: mockUpdate, cancelSilence: jest.fn() }),
}));
jest.mock("@/features/silences/silence-modal", () => ({ SilenceModal: () => null }));
jest.mock("@/shared/lib/hooks/useUserPermissions", () => ({
  useUserPermissions: () => ({ can: () => true }),
}));
jest.mock("@/utils/hooks/useTeams", () => ({
  useTeams: () => ({ data: { teams: [{ id: "alpha" }, { id: "beta" }] } }),
}));

const finite = {
  id: "finite", revision: 3, state: "active", team_id: "alpha", comment: "Finite rule",
  starts_at: "2026-10-04T00:00:00Z", ends_at: "2099-01-01T00:00:00Z",
  selector: { kind: "alert", fingerprints: ["firing"] }, origin: "keep-api",
  created_by: { display_name: "Engineer" },
};

describe("Silences registry", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockMutate.mockResolvedValue(undefined);
    mockUpdate.mockResolvedValue(finite);
    mockUseSilences.mockImplementation((filters) => ({
      silences: [finite, { ...finite, id: "forever", comment: "Indefinite rule", ends_at: null }],
      isLoading: false, error: null, mutate: mockMutate,
      nextCursor: filters.cursor ? null : "second-page",
    }));
  });

  it("uses server pagination and resets it when the team filter changes", () => {
    render(<Silences />);
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(mockUseSilences).toHaveBeenLastCalledWith({ state: "active", team_id: undefined, cursor: "second-page" });
    fireEvent.click(screen.getByRole("button", { name: "Previous" }));
    expect(mockUseSilences).toHaveBeenLastCalledWith({ state: "active", team_id: undefined, cursor: null });
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    fireEvent.change(screen.getByLabelText("Team filter"), { target: { value: "null" } });
    expect(mockUseSilences).toHaveBeenLastCalledWith({ state: "active", team_id: null, cursor: null });
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Team filter"), { target: { value: '"beta"' } });
    expect(mockUseSilences).toHaveBeenLastCalledWith({ state: "active", team_id: "beta", cursor: null });
  });

  it("preserves indefinite rules and extends finite rules from their existing deadline", async () => {
    render(<Silences />);
    const [finiteExtend, indefiniteExtend] = screen.getAllByRole("button", { name: "+4h" });
    expect(indefiniteExtend).toBeDisabled();
    fireEvent.click(indefiniteExtend);
    expect(mockUpdate).not.toHaveBeenCalled();
    fireEvent.click(finiteExtend);
    await waitFor(() => expect(mockUpdate).toHaveBeenCalledWith("finite", 3, { ends_at: "2099-01-01T04:00:00.000Z" }));
  });

  it("disables actions for Alertmanager rules even for an administrator", () => {
    mockUseSilences.mockReturnValue({ silences: [{ ...finite, origin: "alertmanager", read_only: true }],
      isLoading: false, error: null, mutate: mockMutate, nextCursor: null });
    render(<Silences />);
    expect(screen.getByText("Manage in Alertmanager")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "+4h" })).toBeDisabled();
    expect(screen.getAllByTitle("Manage this rule in Alertmanager")).toHaveLength(3);
  });

  it("shows explicit local-only and uncertain synchronization states", () => {
    mockUseSilences.mockReturnValue({ silences: [{ ...finite, synchronization: [
      { source_id: "one", state: "local_only", reason: "unsupported_filter" },
      { source_id: "two", state: "uncertain", reason: "external_result_unknown" },
    ] }], isLoading: false, error: null, mutate: mockMutate, nextCursor: null });
    render(<Silences />);
    expect(screen.getByText("Keep only: no equivalent Alertmanager rule")).toBeInTheDocument();
    expect(screen.getByText("Alertmanager sync needs attention")).toBeInTheDocument();
  });
});
