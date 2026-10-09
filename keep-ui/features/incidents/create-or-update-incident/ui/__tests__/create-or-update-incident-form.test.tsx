import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { CreateOrUpdateIncidentForm } from "../create-or-update-incident-form";
import type { IncidentDto } from "@/entities/incidents/model";

const updateIncident = jest.fn().mockResolvedValue(undefined);
jest.mock("@/entities/incidents/model", () => ({ useIncidentActions: () => ({ updateIncident, addIncident: jest.fn() }) }));
jest.mock("@/entities/users/model/useUsers", () => ({ useUsers: () => ({ data: [] }) }));
jest.mock("next-auth/react", () => ({ useSession: () => ({ data: { user: { email: "engineer" } } }) }));
jest.mock("@/shared/lib/hooks/useUserPermissions", () => ({ useUserPermissions: () => ({ permissions: { role: "responder" } }) }));
jest.mock("@/features/incidents/change-incident-severity", () => ({ IncidentSeveritySelect: () => null }));
jest.mock("next/dynamic", () => () => function Editor({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  return <textarea aria-label="Manual summary" value={value} onChange={(event) => onChange(event.target.value)} />;
});

test("editing other metadata does not adopt generated text or send forbidden policy fields", async () => {
  const incident = { id: "id", user_generated_name: "", user_summary: "", generated_name: "workload: catalog",
    generated_summary: "Generated description", assignee: "engineer", resolve_on: "all_resolved" } as IncidentDto;
  const { container } = render(<CreateOrUpdateIncidentForm incidentToEdit={incident} />);
  expect(screen.getByPlaceholderText("workload: catalog")).toHaveValue("");
  expect(screen.getByLabelText("Manual summary")).toHaveValue("");
  fireEvent.submit(container.querySelector("form")!);
  await waitFor(() => expect(updateIncident).toHaveBeenCalled());
  const body = updateIncident.mock.calls[0][1];
  expect(body).toEqual({ user_generated_name: "", user_summary: "", assignee: "engineer" });
});
