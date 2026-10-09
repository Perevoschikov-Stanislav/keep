import { StoreApi, useStore } from "zustand";
import { FacetsPanelState } from "./create-facets-store";
import { toFacetState, valueToString } from "./utils";
import { useEffect } from "react";
import { FacetState } from "../models";
import { usePathname, useSearchParams } from "next/navigation";
import { getFilterStorageKey, loadSavedFacetsState } from "./filter-persistence";
import { splitFacetValues } from "./use-query-params/split-facet-values";

export function useInitialStateHandler(
  store: StoreApi<FacetsPanelState>,
  panelId?: string
) {
  const facetsConfig = useStore(store, (state) => state.facetsConfig);
  const facets = useStore(store, (state) => state.facets);
  const patchFacetsState = useStore(store, (state) => state.patchFacetsState);

  const isInitialStateHandled = useStore(
    store,
    (state) => state.isInitialStateHandled
  );
  const setIsInitialStateHandled = useStore(
    store,
    (state) => state.setIsInitialStateHandled
  );
  const pathname = usePathname();
  const searchParams = useSearchParams();

  useEffect(() => {
    if (isInitialStateHandled || !facets || !facetsConfig) {
      return;
    }

    const storageKey = getFilterStorageKey(panelId || "default", pathname);
    const savedState =
      loadSavedFacetsState(storageKey) ||
      (panelId === "incidents" || panelId === "incidents-all"
        ? loadSavedFacetsState("keep-filters-incidents")
        : null);

    // Map URL query params if present
    const facetParamMap = new Map<string, string[]>();
    if (searchParams) {
      Array.from(searchParams.entries())
        .filter(([key]) => key.startsWith("facet_"))
        .forEach(([key, value]) => {
          const paramPath = key.replace(/^facet_/, "").toLowerCase();
          facetParamMap.set(paramPath, splitFacetValues(value));
        });
    }

    const facetsStatePatch: FacetState = {};

    facets.forEach((facet) => {
      const facetConfig = facetsConfig?.[facet.id];
      const normalizedPath = facet.property_path
        .replace(/\./g, "_")
        .toLowerCase();

      // 1. Highest priority: URL query params for this facet
      if (facetParamMap.has(normalizedPath)) {
        const values = facetParamMap.get(normalizedPath) || [];
        facetsStatePatch[facet.id] = toFacetState(
          values.map((v) => valueToString(v.replace(/^'|'$/g, "")))
        );
        return;
      }

      // 2. Next priority: Saved filters from localStorage
      if (savedState && typeof savedState === "object") {
        const savedFacet =
          savedState[facet.id] ??
          savedState[facet.property_path] ??
          savedState[facet.name] ??
          savedState[facet.property_path.toLowerCase()] ??
          savedState[facet.name.toLowerCase()];

        if (savedFacet !== undefined && savedFacet !== null) {
          if (Array.isArray(savedFacet)) {
            facetsStatePatch[facet.id] = toFacetState(
              savedFacet.map((v) =>
                valueToString(String(v).replace(/^'|'$/g, ""))
              )
            );
          } else if (typeof savedFacet === "object") {
            const normalizedState: Record<string, boolean> = {};
            Object.keys(savedFacet).forEach((k) => {
              if (savedFacet[k]) {
                normalizedState[valueToString(k.replace(/^'|'$/g, ""))] = true;
              }
            });
            facetsStatePatch[facet.id] = normalizedState;
          }
          return;
        }
      }

      // 3. Fallback: checkedByDefaultOptionValues from facetConfig
      if (facetConfig?.checkedByDefaultOptionValues) {
        facetsStatePatch[facet.id] = toFacetState(
          facetConfig.checkedByDefaultOptionValues.map((value) =>
            valueToString(value)
          )
        );
      }
    });

    setIsInitialStateHandled(true);

    if (Object.entries(facetsStatePatch).length) {
      patchFacetsState(facetsStatePatch);
    }
  }, [
    facetsConfig,
    facets,
    patchFacetsState,
    isInitialStateHandled,
    setIsInitialStateHandled,
    panelId,
    pathname,
    searchParams,
  ]);
}
