/** @jest-environment node */

import { auth } from "@/auth";
import { headers } from "next/headers";
import { getConfig } from "@/shared/lib/server/getConfig";
import { ApiClient } from "../../ApiClient";
import { createServerApiClient } from "../createServerApiClient";

jest.mock("@/auth", () => ({ auth: jest.fn() }));
jest.mock("next/headers", () => ({ headers: jest.fn() }));
jest.mock("@/shared/lib/server/getConfig", () => ({ getConfig: jest.fn() }));
jest.mock("../../ApiClient", () => ({ ApiClient: jest.fn() }));

it("forwards trusted proxy headers without logging their values", async () => {
  const originalEnv = process.env;
  process.env = { ...originalEnv, AUTH_TYPE: "OAUTH2PROXY" };
  for (const name of [
    "KEEP_OAUTH2_PROXY_USER_HEADER", "KEEP_OAUTH2_PROXY_EMAIL_HEADER",
    "KEEP_OAUTH2_PROXY_ACCESS_TOKEN_HEADER", "KEEP_OAUTH2_PROXY_ROLE_HEADER",
  ]) delete process.env[name];
  const proxyHeaders = {
    "x-forwarded-user": "test-user",
    "x-forwarded-email": "test-user@example.test",
    "x-forwarded-access-token": "private-test-token",
    "x-forwarded-groups": "/private-test-group",
  };
  const log = jest.spyOn(console, "log").mockImplementation();
  (auth as jest.Mock).mockResolvedValue(null);
  (getConfig as jest.Mock).mockReturnValue({ AUTH_TYPE: "OAUTH2PROXY" });
  (headers as jest.Mock).mockResolvedValue(new Headers(proxyHeaders));

  try {
    await createServerApiClient();

    expect(ApiClient).toHaveBeenCalledWith(null, { AUTH_TYPE: "OAUTH2PROXY" }, {
      headers: proxyHeaders,
    });
    for (const value of Object.values(proxyHeaders)) {
      expect(JSON.stringify(log.mock.calls)).not.toContain(value);
    }
  } finally {
    log.mockRestore();
    process.env = originalEnv;
  }
});
