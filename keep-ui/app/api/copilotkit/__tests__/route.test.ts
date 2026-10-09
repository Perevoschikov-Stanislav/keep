/** @jest-environment node */

import OpenAI from "openai";
import { NextRequest } from "next/server";
import { copilotRuntimeNextJSAppRouterEndpoint } from "@copilotkit/runtime";
import { POST } from "../route";

const mockHandleRequest = jest.fn();

jest.mock("openai", () => ({
  __esModule: true,
  default: jest.fn(() => ({})),
  OpenAIError: class extends Error {},
}));
jest.mock("@copilotkit/runtime", () => ({
  CopilotRuntime: jest.fn(),
  OpenAIAdapter: jest.fn(),
  copilotRuntimeNextJSAppRouterEndpoint: jest.fn(() => ({
    handleRequest: mockHandleRequest,
  })),
}));

describe("CopilotKit OSS gate", () => {
  const originalEnv = process.env;
  const request = {} as NextRequest;

  beforeEach(() => {
    process.env = { ...originalEnv, OPEN_AI_API_KEY: "test-key" };
    jest.clearAllMocks();
    mockHandleRequest.mockResolvedValue(new Response("handled"));
  });
  afterEach(() => {
    process.env = originalEnv;
  });

  it.each([undefined, "true"])("returns 404 with KEEP_OSS_ONLY=%s even with a key", async (value) => {
    if (value === undefined) delete process.env.KEEP_OSS_ONLY;
    else process.env.KEEP_OSS_ONLY = value;

    const response = await POST(request);

    expect(response.status).toBe(404);
    expect(OpenAI).not.toHaveBeenCalled();
    expect(copilotRuntimeNextJSAppRouterEndpoint).not.toHaveBeenCalled();
    expect(mockHandleRequest).not.toHaveBeenCalled();
  });

  it("handles requests only when AI is explicitly enabled", async () => {
    process.env.KEEP_OSS_ONLY = "false";

    expect(await (await POST(request)).text()).toBe("handled");
    expect(OpenAI).toHaveBeenCalledTimes(1);
    expect(mockHandleRequest).toHaveBeenCalledWith(request);
  });
});
