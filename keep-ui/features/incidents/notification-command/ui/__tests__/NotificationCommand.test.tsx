import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NotificationCommand } from "../NotificationCommand";
import { IncidentDto } from "@/entities/incidents/model/models";

let mockQuery = "command=ack&revision=0";
let mockAllowed = true;
const mockPost = jest.fn();
const mockReplace = jest.fn();
jest.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(mockQuery), usePathname: () => "/incidents/id",
  useRouter: () => ({ replace: mockReplace }),
}));
jest.mock("@/shared/lib/hooks/useApi", () => ({ useApi: () => ({ post: mockPost }) }));
jest.mock("@/shared/lib/hooks/useUserPermissions", () => ({ useUserPermissions: () => ({ can: () => mockAllowed }) }));
jest.mock("@/shared/lib/hooks/useHydratedSession", () => ({ useHydratedSession: () => ({ data: { user: { email: "operator" } } }) }));
jest.mock("@/features/silences/silence-modal/ui/SilenceModal", () => ({ SilenceModal: ({ isOpen }: { isOpen: boolean }) =>
  isOpen ? <div>Silence form</div> : null }));
jest.mock("@/shared/ui", () => ({ showErrorToast: jest.fn() }));

const incident = { id: "id", team_id: "alpha", lifecycle: { revision: 0 } } as IncidentDto;
describe("notification action confirmation", () => {
  beforeEach(() => {
    mockQuery = "command=ack&revision=0"; mockAllowed = true;
    mockPost.mockReset(); mockPost.mockResolvedValue({}); mockReplace.mockReset();
    Object.defineProperty(global.crypto, "randomUUID", { configurable: true, value: () => "test-command-id" });
  });
  it("never executes an action when a notification link is opened", () => {
    render(<NotificationCommand incident={incident} onSuccess={jest.fn()} />);
    expect(screen.getByRole("button", { name: "Acknowledge" })).toBeEnabled();
    expect(mockPost).not.toHaveBeenCalled();
  });
  it("requires a click and sends the displayed canonical revision", async () => {
    const success = jest.fn();
    render(<NotificationCommand incident={incident} onSuccess={success} />);
    fireEvent.click(screen.getByRole("button", { name: "Acknowledge" }));
    await waitFor(() => expect(success).toHaveBeenCalledTimes(1));
    expect(mockPost).toHaveBeenCalledWith("/incidents/id/commands", expect.objectContaining({
      command: "ack", expected_revision: 0, client_request_id: "test-command-id", incident_id: "id",
    }));
    expect(mockReplace).toHaveBeenCalledWith("/incidents/id");
  });
  it("does not permit a viewer or a foreign team to confirm", () => {
    mockAllowed = false;
    render(<NotificationCommand incident={incident} onSuccess={jest.fn()} />);
    expect(screen.getByRole("button", { name: "Acknowledge" })).toBeDisabled();
    expect(mockPost).not.toHaveBeenCalled();
  });
  it("rejects stale and malformed revisions", () => {
    mockQuery = "command=ack&revision=99";
    render(<NotificationCommand incident={incident} onSuccess={jest.fn()} />);
    expect(screen.getByRole("button", { name: "Acknowledge" })).toBeDisabled();
    expect(screen.getByText(/outdated/)).toBeInTheDocument();
  });
  it("retries an unknown HTTP result with the same command ID", async () => {
    mockPost.mockRejectedValueOnce(new Error("Network interrupted"));
    render(<NotificationCommand incident={incident} onSuccess={jest.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Acknowledge" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Acknowledge" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Acknowledge" }));
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(2));
    expect(mockPost.mock.calls[0][1].client_request_id).toBe(mockPost.mock.calls[1][1].client_request_id);
  });
  it("opens the existing silence editor without executing a command", () => {
    mockQuery = "command=silence&revision=0";
    render(<NotificationCommand incident={incident} onSuccess={jest.fn()} />);
    expect(screen.queryByText("Silence form")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Create silence" }));
    expect(screen.getByText("Silence form")).toBeInTheDocument();
    expect(mockPost).not.toHaveBeenCalled();
  });
});
