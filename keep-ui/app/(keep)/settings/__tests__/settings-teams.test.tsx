import { fireEvent, render, screen } from "@testing-library/react";
import { useConfig } from "@/utils/hooks/useConfig";
import { useHydratedSession } from "@/shared/lib/hooks/useHydratedSession";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import SettingsPage from "../settings.client";

jest.mock("@/shared/lib/hooks/useHydratedSession", () => ({
  useHydratedSession: jest.fn(),
}));
jest.mock("next/navigation", () => ({
  useRouter: jest.fn(),
  usePathname: jest.fn(),
  useSearchParams: jest.fn(),
}));
jest.mock("../auth/users-tab", () => ({
  __esModule: true,
  default: () => <div>Users content</div>,
}));
jest.mock("../auth/groups-tab", () => ({
  __esModule: true,
  default: () => null,
}));
jest.mock("../auth/teams-tab", () => ({
  __esModule: true,
  default: () => <div>Teams content</div>,
}));
jest.mock("../auth/roles-tab", () => ({
  __esModule: true,
  default: () => null,
}));
jest.mock("../auth/api-key-tab", () => ({
  __esModule: true,
  default: () => null,
}));
jest.mock("../auth/sso-tab", () => ({ __esModule: true, default: () => null }));
jest.mock("../auth/permissions-tab", () => ({
  __esModule: true,
  default: () => null,
}));
jest.mock("../webhook-settings", () => ({
  __esModule: true,
  default: () => null,
}));
jest.mock("../smtp-settings", () => ({
  __esModule: true,
  default: () => null,
}));
jest.mock("../provider-images/provider-images-settings", () => ({
  __esModule: true,
  default: () => null,
}));

const replace = jest.fn();

describe("Teams tab navigation", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    (useHydratedSession as jest.Mock).mockReturnValue({
      data: { user: { email: "admin@example.test" } },
      status: "authenticated",
    });
    (useConfig as jest.Mock).mockReturnValue({
      data: { AUTH_TYPE: "OAUTH2PROXY", KEEP_OSS_ONLY: true },
    });
    (useRouter as jest.Mock).mockReturnValue({ replace, push: jest.fn() });
    (usePathname as jest.Mock).mockReturnValue("/settings");
    (useSearchParams as jest.Mock).mockReturnValue(new URLSearchParams());
  });

  it("keeps Teams accessible in OSS and routes a tab click", () => {
    render(<SettingsPage />);
    fireEvent.click(screen.getByRole("tab", { name: "Teams" }));
    expect(replace).toHaveBeenCalledWith(
      "/settings?selectedTab=users&userSubTab=teams"
    );
    expect(
      screen.queryByRole("tab", { name: "Groups" })
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "SSO" })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("tab", { name: "Permissions" })
    ).not.toBeInTheDocument();
  });

  it("opens Teams using its direct URL", async () => {
    (useSearchParams as jest.Mock).mockReturnValue(
      new URLSearchParams("selectedTab=users&userSubTab=teams")
    );
    render(<SettingsPage />);
    expect(await screen.findByText("Teams content")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Teams" })).toHaveAttribute(
      "aria-selected",
      "true"
    );
  });

  it("places Teams immediately after Groups when enterprise tabs are enabled", () => {
    (useConfig as jest.Mock).mockReturnValue({
      data: { AUTH_TYPE: "KEYCLOAK", KEEP_OSS_ONLY: false },
    });
    render(<SettingsPage />);
    const labels = screen.getAllByRole("tab").map((tab) => tab.textContent);
    expect(labels[labels.indexOf("Groups") + 1]).toBe("Teams");
  });
});
