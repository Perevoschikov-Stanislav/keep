import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { IncidentChangeStatusSelect } from "../incident-change-status-select";
import { Status } from "@/entities/incidents/model/models";

const mockChangeStatus = jest.fn();
let mockCanEdit = false;

jest.mock("@/entities/incidents/model", () => ({
  ...jest.requireActual("@/entities/incidents/model/models"),
  useIncidentActions: () => ({ changeStatus: mockChangeStatus }),
}));
jest.mock("@/shared/lib/hooks/useUserPermissions", () => ({
  useUserPermissions: () => ({ can: (scope: string) => mockCanEdit && scope === "update:incident" }),
}));
jest.mock("@/shared/ui", () => ({ showErrorToast: jest.fn() }));

describe("incident status permissions", () => {
  beforeEach(() => { mockCanEdit = false; mockChangeStatus.mockClear(); });

  it("disables the control and explains read-only access", () => {
    render(<IncidentChangeStatusSelect incidentId="incident" teamId="team-1" value={Status.Firing} />);
    const input = screen.getByRole("combobox", { name: "Incident status" });
    expect(input).toBeDisabled();
    expect(screen.getByTitle(/Read only/)).toBeInTheDocument();
    fireEvent.mouseDown(input);
    expect(mockChangeStatus).not.toHaveBeenCalled();
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("allows response actions but omits merged/deleted status for responder", () => {
    mockCanEdit = true;
    render(<IncidentChangeStatusSelect incidentId="incident" teamId="team-1" value={Status.Firing} />);
    const input = screen.getByRole("combobox", { name: "Incident status" });
    expect(input).toBeEnabled();
    fireEvent.mouseDown(input);
    expect(screen.getByText("Acknowledged")).toBeInTheDocument();
    expect(screen.queryByText("Merged")).not.toBeInTheDocument();
    expect(screen.queryByText("Deleted")).not.toBeInTheDocument();
  });

  it("sends the current revision with the status update", async () => {
    mockCanEdit = true;
    render(<IncidentChangeStatusSelect incidentId="incident" teamId="team-1" value={Status.Firing} expectedRevision={7} />);
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "Incident status" }));
    fireEvent.click(screen.getByText("Acknowledged"));
    await waitFor(() => expect(mockChangeStatus).toHaveBeenCalledWith("incident", Status.Acknowledged, undefined, 7));
  });

  it("keeps the displayed status when the server rejects a stale revision", async () => {
    mockCanEdit = true;
    mockChangeStatus.mockRejectedValueOnce(new Error("Incident changed"));
    const onChange = jest.fn();
    render(<IncidentChangeStatusSelect incidentId="incident" teamId="team-1" value={Status.Firing} expectedRevision={7} onChange={onChange} />);
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "Incident status" }));
    fireEvent.click(screen.getByText("Resolved"));
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Incident status" })).toBeEnabled());
    expect(onChange).not.toHaveBeenCalled();
  });
});
