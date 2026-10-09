import { useEffect, useMemo, useRef } from "react";
import { StoreApi, useStore } from "zustand";
import { FacetsPanelState } from "../create-facets-store";
import { FacetDto, FacetOptionDto } from "../../models";
import { splitFacetValues } from "./split-facet-values";
import {
  ReadonlyURLSearchParams,
  useSearchParams,
  usePathname,
} from "next/navigation";
import {
  getFilterStorageKey,
  loadSavedFacetsState,
  saveFacetsState,
} from "../filter-persistence";

const facetQueryParamPrefix = "facet_";

function areFacetQueryParamsEqual(
  first: URLSearchParams,
  second: URLSearchParams
): boolean {
  const firstFacetValues = Array.from(first.entries()).filter(([key, value]) =>
    key.startsWith(facetQueryParamPrefix)
  );
  const secondFacetValues = Array.from(second.entries()).filter(
    ([key, value]) => key.startsWith(facetQueryParamPrefix)
  );

  if (firstFacetValues.length !== secondFacetValues.length) {
    return false;
  }
  const firstValuesMap = new Map(firstFacetValues);

  return !secondFacetValues.some(
    ([key, value]) => firstValuesMap.get(key) !== value
  );
}

function buildFacetQueryParams(
  formattedFacets: {
    id: string;
    queryParamName: string;
  }[],
  facetOptions: Record<string, FacetOptionDto[]>,
  facetsState: Record<string, any>
): URLSearchParams {
  const facetQueryParams = new URLSearchParams();

  formattedFacets.forEach((facet) => {
    if (!facetsState[facet.id]) {
      return;
    }

    const facetStateEntries = Object.entries(facetsState[facet.id] || {});
    const facetOptionsCount = facetOptions?.[facet.id]?.length || 0;

    if (facetStateEntries.length === facetOptionsCount) {
      return;
    }

    facetQueryParams.append(
      facet.queryParamName,
      facetStateEntries.map(([key, value]) => key).join(",")
    );
  });

  return facetQueryParams;
}

function replaceQueryParams(searchParams: URLSearchParams): void {
  window.history.replaceState(
    null,
    "",
    `${window.location.pathname}${searchParams.toString() ? "?" + searchParams.toString() : ""}`
  );
}

export function useQueryParams(
  store: StoreApi<FacetsPanelState>,
  panelId?: string
) {
  const searchParamsRef = useRef<ReadonlyURLSearchParams | undefined>(undefined);
  searchParamsRef.current = useSearchParams();
  const pathname = usePathname();
  const storageKey = useMemo(
    () => getFilterStorageKey(panelId || "default", pathname),
    [panelId, pathname]
  );
  const facets = useStore(store, (state) => state.facets);
  const allFacetOptions = useStore(store, (state) => state.facetOptions);
  const allFacetOptionsRef = useRef<Record<string, FacetOptionDto[]> | null>(
    null
  );
  allFacetOptionsRef.current = allFacetOptions;
  const facetsState = useStore(store, (state) => state.facetsState);
  const facetsStateRef = useRef(facetsState);
  facetsStateRef.current = facetsState;
  const facetsStateRefreshToken = useStore(
    store,
    (state) => state.facetsStateRefreshToken
  );

  const isFacetsStateInitializedFromQueryParams = useStore(
    store,
    (state) => state.isFacetsStateInitializedFromQueryParams
  );

  const patchFacetsState = useStore(store, (state) => state.patchFacetsState);
  const setIsFacetsStateInitializedFromQueryParams = useStore(
    store,
    (state) => state.setIsFacetsStateInitializedFromQueryParams
  );
  const isInitialStateHandled = useStore(
    store,
    (state) => state.isInitialStateHandled
  );

  useEffect(() => {
    return () => {
      const baseParams =
        typeof window !== "undefined"
          ? new URLSearchParams(window.location.search)
          : new URLSearchParams(searchParamsRef.current);
      const facetKeys = Array.from(baseParams.keys()).filter((key) =>
        key.startsWith(facetQueryParamPrefix)
      );

      if (facetKeys.length) {
        facetKeys.forEach((key) => baseParams.delete(key));
        replaceQueryParams(baseParams);
      }
    };
  }, [pathname, panelId]);

  const formattedFacets = useMemo(() => {
    if (!facets) {
      return null;
    }

    return facets
      .map((facet: FacetDto) => ({
        id: facet.id,
        name: facet.name,
        property_path: facet.property_path,
        queryParamName:
          facetQueryParamPrefix + facet.property_path.replace(/\./g, "_"),
      }))
      .sort((a, b) => a.queryParamName.localeCompare(b.queryParamName));
  }, [facets]);

  useEffect(() => {
    if (
      !isInitialStateHandled ||
      isFacetsStateInitializedFromQueryParams ||
      !formattedFacets
    ) {
      return;
    }

    const formattedFacetsDict: Record<string, string> = formattedFacets.reduce(
      (acc, curr) => ({ ...acc, [curr.queryParamName]: curr.id }),
      {}
    );
    const facetsStatePatch: Record<string, any> = {};
    const queryParams = new URLSearchParams(searchParamsRef.current);
    const facetEntries = Array.from(queryParams.entries()).filter(([key]) =>
      key.startsWith(facetQueryParamPrefix)
    );

    // If facetsState is empty (e.g. standalone hook execution in tests), apply from URL params or localStorage
    const currentFacetsState = facetsStateRef.current || {};
    const isStateEmpty = Object.keys(currentFacetsState).length === 0;

    if (isStateEmpty && facetEntries.length > 0) {
      facetEntries
        .map(([key, value]) => ({
          facetName: key,
          values: splitFacetValues(value),
        }))
        .forEach(({ facetName, values }) => {
          const facetId = formattedFacetsDict[facetName];
          if (!facetId) return;

          if (!facetsStatePatch[facetId]) {
            facetsStatePatch[facetId] = {};
          }

          values?.forEach((value) => {
            if (!value) {
              return;
            }

            facetsStatePatch[facetId][value] = true;
          });
        });

      patchFacetsState(facetsStatePatch);
      saveFacetsState(storageKey, facetsStatePatch);
    } else if (isStateEmpty) {
      const savedState = loadSavedFacetsState(storageKey);
      if (savedState && typeof savedState === "object") {
        const facetIds = new Set(formattedFacets.map((f) => f.id));
        const facetNameMap = new Map(formattedFacets.map((f) => [f.name, f.id]));
        const facetPropMap = new Map(
          formattedFacets.map((f) => [f.property_path, f.id])
        );
        Object.entries(savedState).forEach(([fKey, opts]) => {
          const targetId =
            (facetIds.has(fKey) ? fKey : undefined) ??
            facetPropMap.get(fKey) ??
            facetNameMap.get(fKey);
          if (targetId && opts && typeof opts === "object") {
            facetsStatePatch[targetId] = opts;
          }
        });
        if (Object.keys(facetsStatePatch).length > 0) {
          patchFacetsState(facetsStatePatch);
        }
      }
    }

    setIsFacetsStateInitializedFromQueryParams(true);
  }, [
    formattedFacets,
    isFacetsStateInitializedFromQueryParams,
    patchFacetsState,
    setIsFacetsStateInitializedFromQueryParams,
    isInitialStateHandled,
    storageKey,
    facets,
  ]);

  // Synchronously persist facets state to localStorage on every change
  useEffect(() => {
    if (!isInitialStateHandled || !facets) {
      return;
    }
    saveFacetsState(storageKey, facetsState);
  }, [
    facetsState,
    facetsStateRefreshToken,
    isInitialStateHandled,
    facets,
    storageKey,
  ]);

  // Debounced sync of facetsState to URL query parameters
  useEffect(() => {
    if (!formattedFacets || !isInitialStateHandled) {
      return;
    }

    const timeoutId = setTimeout(() => {
      const oldQueryParams = new URLSearchParams(searchParamsRef.current);

      const facetQueryParams = buildFacetQueryParams(
        formattedFacets,
        allFacetOptionsRef.current || {},
        facetsStateRef.current
      );

      if (areFacetQueryParamsEqual(facetQueryParams, oldQueryParams)) {
        return;
      }

      Array.from(oldQueryParams.entries())
        .filter(([key, value]) => key.startsWith(facetQueryParamPrefix))
        .forEach(([key]) => oldQueryParams.delete(key));

      Array.from(facetQueryParams.entries()).forEach(([key, value]) =>
        oldQueryParams.append(key, value)
      );

      replaceQueryParams(oldQueryParams);
    }, 300);

    return () => clearTimeout(timeoutId);
  }, [
    formattedFacets,
    facetsStateRefreshToken,
    isInitialStateHandled,
  ]);
}
