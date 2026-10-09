import { GET } from "../route";
import { redirect } from "next/navigation";

jest.mock("next/navigation", () => ({ redirect: jest.fn(() => { throw new Error("NEXT_REDIRECT"); }) }));

describe("incident notification link redirect", () => {
  beforeEach(() => jest.clearAllMocks());

  it("preserves the command and revision until confirmation in Keep", async () => {
    await expect(GET({ url: "https://keep.example.org/incidents/id?command=ack&revision=0" } as Request,
      { params: Promise.resolve({ id: "id" }) })).rejects.toThrow("NEXT_REDIRECT");
    expect(redirect).toHaveBeenCalledWith("/incidents/id/alerts?command=ack&revision=0");
  });

  it("keeps the ordinary incident link working without parameters", async () => {
    await expect(GET({ url: "https://keep.example.org/incidents/id" } as Request,
      { params: Promise.resolve({ id: "id" }) })).rejects.toThrow("NEXT_REDIRECT");
    expect(redirect).toHaveBeenCalledWith("/incidents/id/alerts");
  });
});
