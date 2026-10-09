import { allowsAction } from "../useUserPermissions";

jest.mock("../useApi", () => ({ useApi: jest.fn() }));
jest.mock("@/utils/hooks/useConfig", () => ({ useConfig: jest.fn() }));

describe("permissions for shared visibility", () => {
  const responder = {
    role: "responder", scopes: ["read:*", "update:incident", "update:alert", "execute:workflows"],
    writable_teams: ["team-1"],
  };

  it("allows updates and workflows only on owned objects", () => {
    for (const scope of ["update:incident", "update:alert", "execute:workflows"]) {
      expect(allowsAction(responder, scope, { team_id: "team-1" })).toBe(true);
      expect(allowsAction(responder, scope, { team_id: "other-team" })).toBe(false);
      expect(allowsAction(responder, scope, { team_id: null })).toBe(false);
      expect(allowsAction(responder, scope, {})).toBe(false);
    }
    expect(allowsAction(responder, "delete:incident", { team_id: "team-1" })).toBe(false);
  });

  it("does not grant editing to viewer even with membership", () => {
    expect(allowsAction({ ...responder, role: "viewer", scopes: ["read:*"] }, "update:incident", { team_id: "team-1" })).toBe(false);
  });

  it("allows admin operations on unassigned objects and denies before permissions load", () => {
    expect(allowsAction({ role: "admin", scopes: ["update:*"], writable_teams: null }, "update:incident", { team_id: null })).toBe(true);
    expect(allowsAction(undefined, "update:incident", { team_id: "team-1" })).toBe(false);
  });
});
