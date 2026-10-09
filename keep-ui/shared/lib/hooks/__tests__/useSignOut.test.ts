import { renderHook, act } from "@testing-library/react";
import { useSignOut } from "../useSignOut";
import { signOut } from "next-auth/react";
import { useConfig } from "@/utils/hooks/useConfig";
import { AuthType } from "@/utils/authenticationType";

// Mock dependencies
jest.mock("next-auth/react", () => ({
  signOut: jest.fn(),
}));

jest.mock("@/utils/hooks/useConfig");

jest.mock("@sentry/nextjs", () => ({
  setUser: jest.fn(),
}));

jest.mock("posthog-js", () => ({
  reset: jest.fn(),
}));

describe("useSignOut", () => {
  let locationHref = "";

  beforeEach(() => {
    jest.clearAllMocks();
    locationHref = "";

    // Mock window.location.href using Object.defineProperty
    Object.defineProperty(window, "location", {
      value: {
        href: "",
      },
      writable: true,
      configurable: true,
    });

    Object.defineProperty(window.location, "href", {
      get: () => locationHref,
      set: (value: string) => {
        locationHref = value;
      },
      configurable: true,
    });
  });

  it("should not sign out when config is not loaded", () => {
    (useConfig as jest.Mock).mockReturnValue({ data: null });

    const { result } = renderHook(() => useSignOut());

    act(() => {
      result.current();
    });

    expect(signOut).not.toHaveBeenCalled();
    expect(locationHref).toBe("");
  });

  it("clears the Keep session and redirects to proxy sign out with a login destination", async () => {
    (useConfig as jest.Mock).mockReturnValue({
      data: {
        AUTH_TYPE: AuthType.OAUTH2PROXY,
        SENTRY_DISABLED: "true",
        POSTHOG_DISABLED: "true",
      },
    });

    const { result } = renderHook(() => useSignOut());

    await act(async () => {
      await result.current();
    });

    expect(locationHref).toBe("/oauth2/sign_out?rd=%2Foauth2%2Fsign_in%3Frd%3D%2F");
    expect(signOut).toHaveBeenCalledWith({ redirect: false });
  });

  it("waits for the Keep session to be cleared before leaving for proxy sign out", async () => {
    (useConfig as jest.Mock).mockReturnValue({
      data: { AUTH_TYPE: AuthType.OAUTH2PROXY },
    });
    let completeSignOut!: () => void;
    jest.mocked(signOut).mockImplementationOnce(() => new Promise((resolve) => {
      completeSignOut = () => resolve({ url: "/" });
    }));
    const { result } = renderHook(() => useSignOut());
    await act(async () => {
      const completion = result.current();
      expect(locationHref).toBe("");
      completeSignOut();
      await completion;
    });
    expect(locationHref).toBe("/oauth2/sign_out?rd=%2Foauth2%2Fsign_in%3Frd%3D%2F");
  });

  it("should call NextAuth signOut for DB auth type", () => {
    (useConfig as jest.Mock).mockReturnValue({
      data: {
        AUTH_TYPE: AuthType.DB,
        SENTRY_DISABLED: "true",
        POSTHOG_DISABLED: "true",
      },
    });

    const { result } = renderHook(() => useSignOut());

    act(() => {
      result.current();
    });

    expect(signOut).toHaveBeenCalled();
    expect(locationHref).toBe("");
  });

  it("should call NextAuth signOut for AUTH0 auth type", () => {
    (useConfig as jest.Mock).mockReturnValue({
      data: {
        AUTH_TYPE: AuthType.AUTH0,
        SENTRY_DISABLED: "true",
        POSTHOG_DISABLED: "true",
      },
    });

    const { result } = renderHook(() => useSignOut());

    act(() => {
      result.current();
    });

    expect(signOut).toHaveBeenCalled();
    expect(locationHref).toBe("");
  });

  it("should call NextAuth signOut for KEYCLOAK auth type", () => {
    (useConfig as jest.Mock).mockReturnValue({
      data: {
        AUTH_TYPE: AuthType.KEYCLOAK,
        SENTRY_DISABLED: "true",
        POSTHOG_DISABLED: "true",
      },
    });

    const { result } = renderHook(() => useSignOut());

    act(() => {
      result.current();
    });

    expect(signOut).toHaveBeenCalled();
    expect(locationHref).toBe("");
  });

  it("should call NextAuth signOut for NOAUTH auth type", () => {
    (useConfig as jest.Mock).mockReturnValue({
      data: {
        AUTH_TYPE: AuthType.NOAUTH,
        SENTRY_DISABLED: "true",
        POSTHOG_DISABLED: "true",
      },
    });

    const { result } = renderHook(() => useSignOut());

    act(() => {
      result.current();
    });

    expect(signOut).toHaveBeenCalled();
    expect(locationHref).toBe("");
  });

  it("should reset Sentry user when SENTRY_DISABLED is not true", () => {
    const Sentry = require("@sentry/nextjs");

    (useConfig as jest.Mock).mockReturnValue({
      data: {
        AUTH_TYPE: AuthType.DB,
        SENTRY_DISABLED: "false",
        POSTHOG_DISABLED: "true",
      },
    });

    const { result } = renderHook(() => useSignOut());

    act(() => {
      result.current();
    });

    expect(Sentry.setUser).toHaveBeenCalledWith(null);
  });

  it("should reset PostHog when POSTHOG_DISABLED is not true", () => {
    const posthog = require("posthog-js");

    (useConfig as jest.Mock).mockReturnValue({
      data: {
        AUTH_TYPE: AuthType.DB,
        SENTRY_DISABLED: "true",
        POSTHOG_DISABLED: "false",
      },
    });

    const { result } = renderHook(() => useSignOut());

    act(() => {
      result.current();
    });

    expect(posthog.reset).toHaveBeenCalled();
  });
});
