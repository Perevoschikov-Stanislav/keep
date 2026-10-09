import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { mutate, SWRConfig } from "swr";
import { PresetsNoise } from "../PresetsNoise";

let mockActor = "admin@example.test";
let mockCount = 1;
const mockPost = jest.fn(async () => ({ count: mockCount }));
jest.mock("@/shared/lib/hooks/useApi", () => ({ useApi: () => ({ isReady: () => true, post: mockPost }) }));
jest.mock("@/shared/lib/hooks/useHydratedSession", () => ({ useHydratedSession: () => ({ data: { user: { email: mockActor } } }) }));
jest.mock("@/entities/presets/model", () => ({ useSilencedPresets: () => ({ silencedPresetIds: [] }) }));
jest.mock("next/dynamic", () => () => () => null);

const presets = [{ id: "test-noise", is_noisy: true, options: [{ label: "CEL", value: "true" }] }] as any;

describe("preset sound and silence changes", () => {
  beforeEach(async () => {
    mockActor = "admin@example.test";
    mockCount = 1;
    mockPost.mockClear();
    await mutate(() => true, undefined, { revalidate: false });
  });

  const view = () => <SWRConfig value={{ dedupingInterval: 0 }}><PresetsNoise presets={presets} /></SWRConfig>;

  it("refreshes sound when silence actions invalidate preset caches", async () => {
    render(view());
    await waitFor(() => expect(screen.getByTestId("noisy-presets-audio-player")).toHaveClass("playing"));
    mockCount = 0;
    await mutate((key) => Array.isArray(key) && key[0].startsWith("/preset"));
    await waitFor(() => expect(screen.getByTestId("noisy-presets-audio-player")).not.toHaveClass("playing"));
  });

  it("does not reuse the previous account's noisy count", async () => {
    const page = render(view());
    await waitFor(() => expect(screen.getByTestId("noisy-presets-audio-player")).toHaveClass("playing"));
    mockActor = "viewer@example.test";
    mockCount = 0;
    page.rerender(view());
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId("noisy-presets-audio-player")).not.toHaveClass("playing");
  });
});
