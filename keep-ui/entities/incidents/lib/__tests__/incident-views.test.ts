import { combineIncidentCel } from "../../model/useIncidentViews";

jest.mock("@/shared/lib/hooks/useApi", () => ({ useApi: jest.fn() }));

describe("incident views and table filters", () => {
  it("keeps an OR view inside the status and time filters", () => {
    expect(combineIncidentCel("status == 'firing'", "alert.namespace.startsWith('scanner') || alert.name.startsWith('Scanner')"))
      .toBe("(status == 'firing') && (alert.namespace.startsWith('scanner') || alert.name.startsWith('Scanner'))");
  });
  it("treats ALL as no additional view filter", () => {
    expect(combineIncidentCel("", null, "status == 'firing'"))
      .toBe("(status == 'firing')");
  });
});
