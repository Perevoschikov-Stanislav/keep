import React from "react";
import { render, waitFor } from "@testing-library/react";
import { getProviders, signIn } from "next-auth/react";
import SignInForm from "../SignInForm";

jest.mock("next-auth/react", () => ({ getProviders: jest.fn(), signIn: jest.fn() }));
jest.mock("next/navigation", () => ({ useRouter: () => ({ replace: jest.fn() }) }));
jest.mock("@/app/actions/authactions", () => ({ authenticate: jest.fn(), revalidateAfterAuth: jest.fn() }));

describe("OAUTH2PROXY direct links", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    (getProviders as jest.Mock).mockResolvedValue({ credentials: { id: "credentials", name: "OAuth2Proxy" } });
  });

  it("returns to the requested silence URL after authentication", async () => {
    const callbackUrl = "http://localhost:8012/silences?team_id=cedar";
    render(<SignInForm searchParams={{ callbackUrl }} />);
    await waitFor(() => expect(signIn).toHaveBeenCalledWith("credentials", { callbackUrl }));
  });

  it.each([undefined, ["/silences", "/alerts"]])("falls back for a missing or repeated callback", async (callbackUrl) => {
    render(<SignInForm searchParams={callbackUrl ? { callbackUrl } : {}} />);
    await waitFor(() => expect(signIn).toHaveBeenCalledWith("credentials", { callbackUrl: "/" }));
  });
});
