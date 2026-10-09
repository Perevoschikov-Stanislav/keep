import { middleware } from "../middleware";

jest.mock("@/auth.config", () => ({ config: {} }));
jest.mock("@/utils/apiUrl", () => ({ getApiURL: () => "https://backend.example.org" }));
jest.mock("next-auth", () => ({ __esModule: true, default: () => ({
  auth: (callback?: unknown) => callback ?? Promise.resolve({ userRole: "responder" }),
}) }));
jest.mock("next/server", () => ({ NextResponse: {
  redirect: jest.fn((url: URL) => ({ location: url.toString() })), next: jest.fn(), rewrite: jest.fn(),
} }));

const handler = middleware as unknown as (request: {
  nextUrl: URL; url: string; headers: Headers; auth: object | null;
}) => Promise<{ location: string }>;
const target = "https://keep.example.org/incidents/id?command=ack&revision=0";

describe("notification action through login", () => {
  beforeEach(() => jest.clearAllMocks());

  it("encodes the whole notification URL inside the callback parameter", async () => {
    const result = await handler({ nextUrl: new URL(target), url: target, headers: new Headers(), auth: null });
    const login = new URL(result.location);
    expect(login.pathname).toBe("/signin");
    expect(login.searchParams.get("callbackUrl")).toBe(target);
    expect(login.searchParams.has("revision")).toBe(false);
  });

  it("returns an authenticated user to the complete action link", async () => {
    const result = await handler({ nextUrl: new URL(target), url: target, headers: new Headers(), auth: null });
    const returned = await handler({ nextUrl: new URL(result.location), url: result.location, headers: new Headers(), auth: {} });
    expect(returned.location).toBe(target);
  });
});
