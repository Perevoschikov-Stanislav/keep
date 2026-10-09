import { render, screen } from "@testing-library/react";
import { useCopilotContext } from "@copilotkit/react-core";
import { useConfig } from "@/utils/hooks/useConfig";
import { CreateOrUpdatePresetForm } from "../create-or-update-preset-form";

jest.mock("@/utils/hooks/useConfig", () => ({ useConfig: jest.fn() }));
jest.mock("@/utils/hooks/useTags", () => ({ useTags: () => ({ data: [] }) }));
jest.mock("@/entities/presets/model/usePresetActions", () => ({
  usePresetActions: () => ({ createPreset: jest.fn(), updatePreset: jest.fn() }),
}));
jest.mock("../preset-controls", () => ({ PresetControls: () => null }));
jest.mock("../alerts-count-badge", () => ({ AlertsCountBadge: () => null }));
jest.mock("@/components/ui/CreatableMultiSelect", () => ({
  __esModule: true, default: () => null,
}));
jest.mock("@copilotkit/react-core", () => ({
  CopilotKit: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="copilot-provider">{children}</div>
  ),
  useCopilotContext: jest.fn(() => ({})),
  useCopilotReadable: jest.fn(),
  useCopilotAction: jest.fn(),
  CopilotTask: jest.fn(),
}));

const props = {
  presetId: null,
  presetData: {
    CEL: "severity == 'critical'", name: "Test preset", isPrivate: false,
    isNoisy: false, counterShowsFiringOnly: true, tags: [], groupColumn: "",
  },
  groupableColumns: [],
};

describe("Preset form OSS mode", () => {
  beforeEach(() => jest.clearAllMocks());

  it.each([undefined, true])("does not initialize AI with KEEP_OSS_ONLY=%s even with a key", (ossOnly) => {
    (useConfig as jest.Mock).mockReturnValue({
      data: { KEEP_OSS_ONLY: ossOnly, OPEN_AI_API_KEY_SET: true },
    });

    render(<CreateOrUpdatePresetForm {...props} />);

    expect(screen.getByTestId("preset-name-input")).toHaveValue("Test preset");
    expect(screen.queryByRole("button", { name: "AI" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("copilot-provider")).not.toBeInTheDocument();
    expect(useCopilotContext).not.toHaveBeenCalled();
  });

  it("initializes AI only when enabled and a key is configured", () => {
    (useConfig as jest.Mock).mockReturnValue({
      data: { KEEP_OSS_ONLY: false, OPEN_AI_API_KEY_SET: true },
    });

    render(<CreateOrUpdatePresetForm {...props} />);

    expect(screen.getByTestId("copilot-provider")).toContainElement(
      screen.getByRole("button", { name: "AI" })
    );
  });
});
