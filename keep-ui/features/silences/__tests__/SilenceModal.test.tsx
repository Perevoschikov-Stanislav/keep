import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { SilenceModal } from "@/features/silences/silence-modal";
import { AlertDto, Severity, Status } from "@/entities/alerts/model";
import { IncidentDto } from "@/entities/incidents/model";

const mockCreateSilence = jest.fn();
const mockUpdateSilence = jest.fn();
jest.mock("@/components/ui/Modal", () => ({ isOpen, title, children }: any) =>
  isOpen ? <section role="dialog" aria-label={title}>{children}</section> : null);
jest.mock("react-datepicker", () => (props: any) => <input
  placeholder={props.placeholderText || "End date and time"}
  value={props.selected?.toISOString() || ""}
  onChange={(event) => props.onChange(event.target.value ? new Date(event.target.value) : null)} />);
jest.mock("@/entities/silences/model", () => ({
  ...jest.requireActual("@/entities/silences/model"),
  useSilenceActions: () => ({
    createSilence: mockCreateSilence,
    updateSilence: mockUpdateSilence,
  }),
}));

const mockCan = jest.fn();
jest.mock("@/shared/lib/hooks/useUserPermissions", () => ({
  useUserPermissions: () => ({
    can: mockCan,
    permissions: { writable_teams: null },
  }),
}));

jest.mock("@/utils/hooks/useTeams", () => ({
  useTeams: () => ({
    data: { teams: [{ id: "team-a" }, { id: "team-b" }] },
  }),
}));

describe("SilenceModal", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockCan.mockReturnValue(true);
    mockCreateSilence.mockResolvedValue({ id: "new-silence-1" });
    mockUpdateSilence.mockResolvedValue({ id: "existing-silence" });
  });

  const sampleAlert: AlertDto = {
    id: "alert-1",
    event_id: "11111111-1111-4111-8111-111111111111",
    fingerprint: "fp-12345",
    team_id: "team-a",
    name: "HighMemoryUsage",
    description: "Memory threshold exceeded",
    severity: "high" as any,
    status: "firing" as any,
    source: ["prometheus"],
    lastReceived: new Date(),
    environment: "production",
    pushed: true,
    deleted: false,
    dismissed: false,
    enriched_fields: [],
    ticket_url: "",
  };

  const sampleIncident: IncidentDto = {
    id: "11111111-1111-1111-1111-111111111111",
    team_id: "team-a",
    user_generated_name: "Database Failover",
    ai_generated_name: "",
    user_summary: "Main DB primary failed over",
    generated_summary: "",
    assignee: "alice",
    severity: "high" as any,
    status: "firing" as any,
    alerts_count: 3,
    alert_sources: ["prometheus"],
    services: ["db"],
    creation_time: new Date(),
    is_candidate: false,
    rule_fingerprint: "",
    same_incident_in_the_past_id: "",
    following_incidents_ids: [],
    merged_into_incident_id: "",
    merged_by: "",
    merged_at: new Date(),
    fingerprint: "inc-fp",
    enrichments: {},
    resolve_on: "all_resolved",
  };

  it("creates silence rule from alert with prefilled fingerprint", async () => {
    const handleClose = jest.fn();
    const handleSuccess = jest.fn();

    render(
      <SilenceModal
        isOpen={true}
        onClose={handleClose}
        alerts={[sampleAlert]}
        onSuccess={handleSuccess}
      />
    );

    expect(screen.getByText("Silence Alert Notifications")).toBeInTheDocument();
    expect(screen.getByText(/fp-12345/)).toBeInTheDocument();

    // Fill in comment
    const commentInput = screen.getByPlaceholderText(/Scheduled maintenance on payment gateway/i);
    fireEvent.change(commentInput, { target: { value: "Investigating memory leak" } });

    // Select Indefinite / Forever duration
    const foreverBtn = screen.getByRole("button", { name: "Indefinite" });
    fireEvent.click(foreverBtn);

    // Submit form
    const applyBtn = screen.getByRole("button", { name: "Apply Silence" });
    fireEvent.click(applyBtn);

    await waitFor(() => {
      expect(mockCreateSilence).toHaveBeenCalledWith(
        expect.objectContaining({
          selector: { kind: "alert", fingerprints: ["fp-12345"] },
          ends_at: null,
          starts_at: null,
          comment: "Investigating memory leak",
        })
      );
      expect(handleSuccess).toHaveBeenCalled();
      expect(handleClose).toHaveBeenCalled();
    });
  });

  it("creates silence rule from incident with prefilled incident ID and team", async () => {
    const handleClose = jest.fn();
    const handleSuccess = jest.fn();

    render(
      <SilenceModal
        isOpen={true}
        onClose={handleClose}
        incident={sampleIncident}
        onSuccess={handleSuccess}
      />
    );

    expect(screen.getByText("Silence Incident Notifications")).toBeInTheDocument();
    expect(screen.getByText("11111111-1111-1111-1111-111111111111")).toBeInTheDocument();

    const commentInput = screen.getByPlaceholderText(/Scheduled maintenance on payment gateway/i);
    fireEvent.change(commentInput, { target: { value: "Planned switchover" } });

    const applyBtn = screen.getByRole("button", { name: "Apply Silence" });
    fireEvent.click(applyBtn);

    await waitFor(() => {
      expect(mockCreateSilence).toHaveBeenCalledWith(
        expect.objectContaining({
          team_id: "team-a",
          selector: {
            kind: "incident",
            incident_ids: ["11111111-1111-1111-1111-111111111111"],
          },
          comment: "Planned switchover",
        })
      );
      expect(handleSuccess).toHaveBeenCalled();
      expect(handleClose).toHaveBeenCalled();
    });
  });

  it("shows validation error when comment is empty", async () => {
    render(
      <SilenceModal
        isOpen={true}
        onClose={jest.fn()}
        alerts={[sampleAlert]}
      />
    );

    const applyBtn = screen.getByRole("button", { name: "Apply Silence" });
    fireEvent.click(applyBtn);

    await waitFor(() => {
      expect(screen.getByText("Comment / reason is required")).toBeInTheDocument();
      expect(mockCreateSilence).not.toHaveBeenCalled();
    });
  });

  it("disables apply button when user lacks write:silence permission", () => {
    mockCan.mockReturnValue(false);

    render(
      <SilenceModal
        isOpen={true}
        onClose={jest.fn()}
        alerts={[sampleAlert]}
      />
    );

    const applyBtn = screen.getByRole("button", { name: "Apply Silence" });
    expect(applyBtn).toBeDisabled();
    expect(
      screen.getByText(/You do not have permission to manage silences for this team/i)
    ).toBeInTheDocument();
  });

  it("rejects bulk targets from different teams", () => {
    render(<SilenceModal isOpen onClose={jest.fn()}
      alerts={[sampleAlert, { ...sampleAlert, fingerprint: "b", team_id: "team-b" }]} />);
    expect(screen.getByText("Select alerts from one team to create a silence.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Apply Silence" })).toBeDisabled();
  });

  it("preserves entered values when the same alert is refreshed", () => {
    const { rerender } = render(<SilenceModal isOpen onClose={jest.fn()} alerts={[sampleAlert]} />);
    const comment = screen.getByPlaceholderText(/Scheduled maintenance on payment gateway/i);
    fireEvent.change(comment, { target: { value: "Keep this reason" } });
    rerender(<SilenceModal isOpen onClose={jest.fn()} alerts={[{ ...sampleAlert, description: "updated" }]} />);
    expect(comment).toHaveValue("Keep this reason");
  });

  it("supports a future start and preserves an absolute interval", async () => {
    render(<SilenceModal isOpen onClose={jest.fn()} alerts={[sampleAlert]} />);
    fireEvent.change(screen.getByLabelText("Start"), { target: { value: "scheduled" } });
    fireEvent.change(screen.getByPlaceholderText("Start date and time"), { target: { value: "2030-01-01T12:00:00Z" } });
    fireEvent.change(screen.getByPlaceholderText(/Scheduled maintenance on payment gateway/i), { target: { value: "planned" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply Silence" }));
    await waitFor(() => expect(mockCreateSilence).toHaveBeenCalledWith(expect.objectContaining({
      starts_at: "2030-01-01T12:00:00.000Z", ends_at: "2030-01-01T16:00:00.000Z",
    })));
  });

  it("edits a filter selector through the actual update command", async () => {
    render(<SilenceModal isOpen onClose={jest.fn()} silenceToEdit={{
      id: "existing", revision: 4, team_id: "team-a", state: "active",
      selector: { kind: "filter", cel: "service == 'old'" }, comment: "reason",
      starts_at: "2020-01-01T00:00:00Z", ends_at: null,
    } as any} />);
    fireEvent.change(screen.getByLabelText("Common Expression Language (CEL) expression"), { target: { value: "service == 'new'" } });
    fireEvent.click(screen.getByRole("button", { name: "Save Changes" }));
    await waitFor(() => expect(mockUpdateSilence).toHaveBeenCalledWith("existing", 4,
      expect.objectContaining({ selector: { kind: "filter", cel: "service == 'new'" } })));
  });

  it("blocks editing externally managed rules including direct form submission", () => {
    render(<SilenceModal isOpen onClose={jest.fn()} silenceToEdit={{
      id: "imported", revision: 1, team_id: "team-a", state: "active", origin: "alertmanager", read_only: true,
      selector: { kind: "filter", cel: "labels.alertname == 'A'" }, comment: "source rule",
      starts_at: "2020-01-01T00:00:00Z", ends_at: null,
    } as any} />);
    const submit = screen.getByRole("button", { name: "Save Changes" });
    expect(submit).toBeDisabled();
    expect(screen.getByText("Manage this rule in Alertmanager.")).toBeInTheDocument();
    fireEvent.submit(submit.closest("form")!);
    expect(mockUpdateSilence).not.toHaveBeenCalled();
  });
});
