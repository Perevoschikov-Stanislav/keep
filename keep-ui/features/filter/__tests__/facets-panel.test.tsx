import React from "react";
import { render, screen } from "@testing-library/react";
import { FacetsPanel } from "../facets-panel";
import { FacetDto } from "../models";
import { useConfig } from "@/utils/hooks/useConfig";

jest.mock("../facet", () => ({
  Facet: ({ facet }: { facet: FacetDto }) => (
    <div data-testid="facet">{facet.name}</div>
  ),
}));

jest.mock("../edit-facet-modal", () => ({ EditFacetModal: () => null }));
jest.mock("../store/use-query-params/use-query-params", () => ({
  useQueryParams: jest.fn(),
}));

const makeFacet = (name: string, order?: number | null): FacetDto => ({
  id: name,
  name,
  property_path: name.toLowerCase(),
  is_static: false,
  is_lazy: true,
  order,
});

function showFacets(facets: FacetDto[]) {
  render(
    <FacetsPanel
      panelId="test"
      className=""
      facets={facets}
      facetOptions={{}}
      onAddFacet={jest.fn()}
      onDeleteFacet={jest.fn()}
      onLoadFacetOptions={jest.fn()}
      onReloadFacetOptions={jest.fn()}
    />
  );
  return screen.getAllByTestId("facet").map((item) => item.textContent);
}

beforeEach(() => {
  localStorage.clear();
  jest
    .mocked(useConfig)
    .mockReturnValue({ data: {} } as ReturnType<typeof useConfig>);
});

it("renders loading facets and accepts the data when the request completes", () => {
  const props = {
    panelId: "loading",
    className: "",
    facetOptions: {},
    onAddFacet: jest.fn(),
    onDeleteFacet: jest.fn(),
    onLoadFacetOptions: jest.fn(),
    onReloadFacetOptions: jest.fn(),
  };
  const { rerender } = render(<FacetsPanel {...props} facets={undefined} />);
  expect(screen.getAllByTestId("facet")).toHaveLength(3);
  rerender(<FacetsPanel {...props} facets={[makeFacet("Zone", null)]} />);
  expect(screen.getAllByTestId("facet").map((item) => item.textContent)).toEqual(["Zone"]);
});

it("applies the default order when the API returns null orders", () => {
  expect(
    showFacets([
      makeFacet("Severity", null),
      makeFacet("Status", null),
      makeFacet("Namespace", null),
      makeFacet("Cluster", null),
      makeFacet("Zone", null),
    ])
  ).toEqual(["Zone", "Cluster", "Namespace", "Severity", "Status"]);
});

it("keeps explicit orders ahead of unordered facets, including order zero", () => {
  expect(
    showFacets([
      makeFacet("Zone", null),
      makeFacet("Second", 2),
      makeFacet("First", 0),
      makeFacet("Cluster"),
    ])
  ).toEqual(["First", "Second", "Zone", "Cluster"]);
});

it("keeps the saved user order ahead of explicit and default orders", () => {
  localStorage.setItem(
    "keephq-facets-order-test",
    JSON.stringify(["Severity", "Zone"])
  );
  expect(
    showFacets([
      makeFacet("Cluster", 0),
      makeFacet("Zone", null),
      makeFacet("Severity", null),
    ])
  ).toEqual(["Severity", "Zone", "Cluster"]);
});

it("uses the configured default order for null orders", () => {
  jest.mocked(useConfig).mockReturnValue({
    data: { DEFAULT_FACETS_ORDER: ["Namespace", "Zone"] },
  } as ReturnType<typeof useConfig>);
  expect(
    showFacets([
      makeFacet("Zone", null),
      makeFacet("Cluster", null),
      makeFacet("Namespace", null),
    ])
  ).toEqual(["Namespace", "Zone", "Cluster"]);
});
