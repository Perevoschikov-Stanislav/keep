import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { UnsilenceModal } from "@/features/silences/silence-modal";
import { SilenceReason } from "@/entities/silences/model";

const mockCancelSilence = jest.fn();
jest.mock("@/entities/silences/model", () => ({
  ...jest.requireActual("@/entities/silences/model"),
  useSilenceActions: () => ({
    cancelSilence: mockCancelSilence,
  }),
}));

const mockCan = jest.fn();
jest.mock("@/shared/lib/hooks/useUserPermissions", () => ({
  useUserPermissions: () => ({
    can: mockCan,
  }),
}));

describe("UnsilenceModal", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockCan.mockReturnValue(true);
    mockCancelSilence.mockResolvedValue({ id: "rule-1" });
  });

  const sampleReasons: SilenceReason[] = [
    {
      silence_id: "11111111-1111-1111-1111-111111111111",
      revision: 2,
      via: "fingerprint",
      incident_id: null,
      ends_at: "2026-10-05T12:00:00Z",
    },
    {
      silence_id: "22222222-2222-2222-2222-222222222222",
      revision: 1,
      via: "filter",
      incident_id: null,
      ends_at: null,
    },
  ];

  it("renders multiple active rules and selects specific one for cancellation", async () => {
    const handleClose = jest.fn();
    const handleSuccess = jest.fn();

    render(
      <UnsilenceModal
        isOpen={true}
        onClose={handleClose}
        targetName="PaymentServiceDown"
        teamId="team-a"
        reasons={sampleReasons}
        onSuccess={handleSuccess}
      />
    );

    expect(screen.getByText("Unsilence: PaymentServiceDown")).toBeInTheDocument();
    expect(screen.getByText("Multiple Active Silences")).toBeInTheDocument();

    // Select second rule
    const radios = screen.getAllByRole("radio");
    expect(radios).toHaveLength(2);
    fireEvent.click(radios[1]);

    // Submit form
    const submitBtn = screen.getByRole("button", { name: /Cancel Silence Rule/i });
    fireEvent.click(submitBtn);

    await waitFor(() => {
      expect(mockCancelSilence).toHaveBeenCalledWith(
        "22222222-2222-2222-2222-222222222222",
        1,
        "Manual unsilence via UI"
      );
      expect(handleSuccess).toHaveBeenCalled();
      expect(handleClose).toHaveBeenCalled();
    });
  });

  it("disables submit button when user lacks update:silence permission", () => {
    mockCan.mockReturnValue(false);

    render(
      <UnsilenceModal
        isOpen={true}
        onClose={jest.fn()}
        targetName="PaymentServiceDown"
        teamId="team-a"
        reasons={sampleReasons}
      />
    );

    const submitBtn = screen.getByRole("button", { name: /Cancel Silence Rule/i });
    expect(submitBtn).toBeDisabled();
  });

  it("keeps Alertmanager rules read only and selects a locally editable rule", async () => {
    render(<UnsilenceModal isOpen onClose={jest.fn()} teamId="team-a"
      reasons={[{ ...sampleReasons[0], read_only: true }, sampleReasons[1]]} />);
    const radios = screen.getAllByRole("radio");
    expect(radios[0]).toBeDisabled();
    expect(radios[1]).toBeChecked();
    expect(screen.getByText("Manage this rule in Alertmanager.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cancel Silence Rule" }));
    await waitFor(() => expect(mockCancelSilence).toHaveBeenCalledWith(sampleReasons[1].silence_id,
      sampleReasons[1].revision, "Manual unsilence via UI"));
  });

  it("disables cancellation when all rules are externally managed", () => {
    render(<UnsilenceModal isOpen onClose={jest.fn()} teamId="team-a"
      reasons={[{ ...sampleReasons[0], read_only: true }]} />);
    expect(screen.getByRole("button", { name: "Cancel Silence Rule" })).toBeDisabled();
    fireEvent.submit(screen.getByRole("button", { name: "Cancel Silence Rule" }).closest("form")!);
    expect(mockCancelSilence).not.toHaveBeenCalled();
  });
});
