import { render, screen, within } from "@testing-library/react";
import { SWRConfig } from "swr";
import { useApi } from "@/shared/lib/hooks/useApi";
import { useHydratedSession } from "@/shared/lib/hooks/useHydratedSession";
import TeamsTab from "../teams-tab";

jest.mock("@/shared/lib/hooks/useHydratedSession", () => ({
  useHydratedSession: jest.fn(),
}));

const alpha = {
  id: "service-desk",
  groups: ["/org/service-desk"],
  zones: ["rack-42"],
  visible_to: ["service-desk"],
};
const beta = {
  id: "engineering",
  groups: ["/org/engineering"],
  zones: ["rack-99"],
  visible_to: ["engineering", "service-desk"],
};
const api = { isReady: () => true, get: jest.fn() };

function renderTab() {
  const cache = new Map();
  const page = () => (
    <SWRConfig
      value={{
        provider: () => cache,
        dedupingInterval: 0,
        shouldRetryOnError: false,
      }}
    >
      <TeamsTab />
    </SWRConfig>
  );
  const view = render(page());
  return { ...view, refresh: () => view.rerender(page()) };
}

describe("Teams settings", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    (useApi as jest.Mock).mockReturnValue(api);
    (useHydratedSession as jest.Mock).mockReturnValue({
      data: { user: { email: "admin@example.test" } },
      status: "authenticated",
    });
    api.get.mockResolvedValue({
      enabled: true,
      visibility: "team",
      teams: [alpha, beta],
    });
  });

  it("shows IaC groups, zones and readers for arbitrary team IDs", async () => {
    renderTab();
    const group = await screen.findByText("/org/service-desk");
    const row = group.closest("tr")!;
    expect(within(row).getByText("rack-42")).toBeInTheDocument();
    expect(within(row).getAllByText("service-desk")).toHaveLength(2);
    expect(screen.getByText("/org/engineering")).toBeInTheDocument();
    expect(screen.getByText(/managed through IaC/)).toBeInTheDocument();
    expect(screen.getByText(/Team visibility:/)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /add|edit|delete/i })
    ).not.toBeInTheDocument();
    expect(api.get).toHaveBeenCalledWith("/auth/teams");
  });

  it("explains shared reading without granting editing", async () => {
    api.get.mockResolvedValue({
      enabled: true,
      visibility: "all",
      teams: [alpha, beta],
    });
    renderTab();
    expect(await screen.findByText(/Shared visibility:/)).toHaveTextContent(
      "Responders can change only their own teams' events."
    );
    expect(screen.getAllByText("All roles")).toHaveLength(2);
  });

  it("shows the active IaC version, source and configuration drift", async () => {
    const configuration = {
      generation: 3,
      revision: "rollout-3",
      digest: "a".repeat(64),
      source: "version:1",
      applied_by: "operator@example.test",
      applied_at: "2026-10-05T08:00:00Z",
      result: "applied",
      drift_count: 2,
    };
    api.get.mockResolvedValue({ enabled: true, visibility: "team", teams: [alpha], configuration });
    renderTab();
    expect(await screen.findByText("Active IaC configuration")).toBeInTheDocument();
    expect(screen.getByText(/Revision: rollout-3/)).toHaveTextContent("Generation: 3");
    expect(screen.getByText("Source: version:1")).toBeInTheDocument();
    expect(screen.getByText(/Applied by operator@example.test/)).toBeInTheDocument();
    expect(screen.getByText("Last apply: applied")).toBeInTheDocument();
    expect(screen.getByText("Resources with drift: 2")).toBeInTheDocument();
    expect(screen.getByText("Digest: " + configuration.digest)).toBeInTheDocument();
  });

  it("distinguishes no accessible teams from unconfigured isolation", async () => {
    api.get.mockResolvedValue({ enabled: true, visibility: "team", teams: [] });
    renderTab();
    expect(
      await screen.findByText("No teams are available to your account.")
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Team isolation is not configured.")
    ).not.toBeInTheDocument();
  });

  it("shows an unconfigured state without sample teams", async () => {
    api.get.mockResolvedValue({ enabled: false, visibility: null, teams: [] });
    renderTab();
    expect(
      await screen.findByText("Team isolation is not configured.")
    ).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("shows a request failure instead of invented data", async () => {
    api.get.mockRejectedValue(new Error("Request failed"));
    renderTab();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Unable to load teams."
    );
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("does not show the previous user's cached teams after switching accounts", async () => {
    const view = renderTab();
    expect(await screen.findByText("/org/engineering")).toBeInTheDocument();

    (useHydratedSession as jest.Mock).mockReturnValue({
      data: { user: { email: "viewer@example.test" } },
      status: "authenticated",
    });
    api.get.mockResolvedValue({
      enabled: true,
      visibility: "team",
      teams: [alpha],
    });
    view.refresh();

    expect(screen.queryByText("/org/engineering")).not.toBeInTheDocument();
    expect(await screen.findByText("/org/service-desk")).toBeInTheDocument();
    expect(screen.queryByText("/org/engineering")).not.toBeInTheDocument();
  });
});
