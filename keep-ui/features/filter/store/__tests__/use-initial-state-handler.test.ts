import { useInitialStateHandler } from "../use-initial-state-handler";

import { StoreApi } from "zustand";
import { renderHook } from "@testing-library/react";
import {
  createFacetsPanelStore,
  FacetsPanelState,
} from "../create-facets-store";
import { FacetConfig, FacetDto, FacetsConfig } from "@/features/filter/models";
import { useFacetsConfig } from "../use-facets-config";

describe("useInitialStateHandler", () => {
  let store: StoreApi<FacetsPanelState>;

  beforeEach(() => {
    store = createFacetsPanelStore();
    store.setState({
      facets: [
        {
          id: "severityFacet",
          name: "Severity",
          property_path: "severity",
        } as FacetDto,
        {
          id: "statusFacet",
          name: "Status",
          property_path: "status",
        } as FacetDto,
      ],
      facetOptions: null,
      facetsState: {},
      isInitialStateHandled: false,
    });
    const facetsConfig: FacetsConfig = {
      Status: {
        checkedByDefaultOptionValues: ["firing", "acknowledged"],
      } as FacetConfig,
    };

    renderHook(() => useFacetsConfig(facetsConfig, store));
    renderHook(() => useInitialStateHandler(store));
  });

  it("should set default option values for status facet", () => {
    expect(store.getState().facetsState).toEqual(
      expect.objectContaining({
        statusFacet: {
          "'firing'": true,
          "'acknowledged'": true,
        },
      })
    );
  });

  it("should not set default option values for severity facet", () => {
    expect(store.getState().facetsState).not.toEqual(
      expect.objectContaining({
        severityFacet: expect.anything(),
      })
    );
  });

  it("should restore saved state from localStorage instead of defaults", () => {
    const savedStore = createFacetsPanelStore();
    savedStore.setState({
      facets: [
        {
          id: "statusFacet",
          name: "Status",
          property_path: "status",
        } as FacetDto,
      ],
      facetOptions: null,
      facetsState: {},
      isInitialStateHandled: false,
    });
    const facetsConfig: FacetsConfig = {
      Status: {
        checkedByDefaultOptionValues: ["firing", "acknowledged"],
      } as FacetConfig,
    };

    window.localStorage.setItem(
      "keep-filters-incidents",
      JSON.stringify({ statusFacet: { "'resolved'": true } })
    );

    renderHook(() => useFacetsConfig(facetsConfig, savedStore));
    renderHook(() => useInitialStateHandler(savedStore, "incidents"));

    expect(savedStore.getState().facetsState).toEqual({
      statusFacet: {
        "'resolved'": true,
      },
    });
  });
});
