import { FacetDto, FacetState } from "../models";

/**
 * Returns the localStorage key for persisting facet filters.
 * Scoped by panelId, preset name (for alerts), and viewId (for incidents).
 */
export function getFilterStorageKey(
  panelId: string,
  pathname?: string | null,
  viewId?: string | null
): string {
  const normalizedPanel = (panelId || "default").toLowerCase();
  if (normalizedPanel.startsWith("incidents")) {
    if (normalizedPanel !== "incidents") {
      return `keep-filters-${normalizedPanel}`;
    }
    if (viewId) {
      return `keep-filters-incidents-${viewId.toLowerCase()}`;
    }
    return `keep-filters-incidents`;
  }
  if (normalizedPanel === "alerts") {
    // Extract preset name from pathname (e.g. /alerts/feed -> feed)
    const segments = (pathname || "")
      .replace(/^\/+/, "")
      .split("/")
      .filter(Boolean);
    const preset = segments.length > 1 ? segments[1] : "feed";
    return `keep-filters-alerts-${preset.toLowerCase()}`;
  }
  return `keep-filters-${normalizedPanel}`;
}

/**
 * Safely loads saved facet filter selections from localStorage.
 */
export function loadSavedFacetsState(storageKey: string): FacetState | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    const raw = window.localStorage.getItem(storageKey);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      return null;
    }
    return parsed as FacetState;
  } catch {
    return null;
  }
}

/**
 * Persists facet filter selections to localStorage.
 * If empty or invalid, removes the entry to save space.
 */
export function saveFacetsState(
  storageKey: string,
  facetsState: FacetState
): void {
  if (typeof window === "undefined") {
    return;
  }
  try {
    if (!facetsState || typeof facetsState !== "object") {
      window.localStorage.removeItem(storageKey);
      return;
    }

    const hasAnyValues = Object.values(facetsState).some(
      (facetMap) =>
        facetMap &&
        (Array.isArray(facetMap)
          ? facetMap.length > 0
          : Object.keys(facetMap).length > 0)
    );
    if (!hasAnyValues) {
      window.localStorage.removeItem(storageKey);
      return;
    }

    window.localStorage.setItem(storageKey, JSON.stringify(facetsState));
  } catch {
    // Ignore storage quota or security errors
  }
}

/**
 * Removes saved facet filter selections from localStorage.
 */
export function clearSavedFacetsState(storageKey: string): void {
  if (typeof window === "undefined") {
    return;
  }
  try {
    window.localStorage.removeItem(storageKey);
  } catch {
    // Ignore
  }
}

function extractFacetValues(entry: any): string[] {
  if (!entry) return [];
  if (Array.isArray(entry)) {
    return entry.map((v) => `'${String(v).replace(/^'|'$/g, "")}'`);
  }
  if (typeof entry === "object") {
    return Object.keys(entry)
      .filter((k) => entry[k])
      .map((k) => `'${String(k).replace(/^'|'$/g, "")}'`);
  }
  return [];
}

/**
 * Constructs an initial CEL string from saved facet filters.
 * Used on mount before facets metadata is loaded from backend to prevent
 * flash of unfiltered items or double-requests.
 */
export function buildCelFromSavedFilters(
  savedState: FacetState,
  initialFacets?: FacetDto[],
  defaultStatuses?: string[]
): string | null {
  if (!savedState || typeof savedState !== "object") {
    return null;
  }

  const parts: string[] = [];

  // 1. Status facet - always first
  const statusFacet = initialFacets?.find(
    (f) =>
      f.property_path === "status" || f.name.toLowerCase() === "status"
  );
  const statusState =
    (statusFacet ? savedState[statusFacet.id] : undefined) ??
    savedState["Status"] ??
    savedState["status"] ??
    savedState["1e7b1d6e-1c2b-4f8e-9f8e-1c2b4f8e9f8e"] ??
    savedState["5dd1519c-6277-4109-ad95-c19d2f4f15e3"];

  if (statusState && typeof statusState === "object") {
    const statuses = extractFacetValues(statusState);
    if (statuses.length > 0) {
      parts.push(`(status in [${statuses.join(", ")}])`);
    }
  } else if (
    !statusState &&
    !("status" in savedState) &&
    !("Status" in savedState) &&
    !(statusFacet && statusFacet.id in savedState)
  ) {
    if (defaultStatuses && defaultStatuses.length > 0) {
      parts.push(
        `(status in [${defaultStatuses.map((opt) => "'" + opt + "'").join(", ")}])`
      );
    }
  }

  // 2. Other facets if initialFacets is provided
  if (initialFacets && Array.isArray(initialFacets)) {
    initialFacets.forEach((facet) => {
      if (facet.property_path === "status") return;
      const facetState =
        savedState[facet.id] ??
        savedState[facet.name] ??
        savedState[facet.property_path];
      if (facetState && typeof facetState === "object") {
        const values = extractFacetValues(facetState);
        if (values.length > 0) {
          parts.push(`(${facet.property_path} in [${values.join(", ")}])`);
        }
      }
    });
  }

  return parts.length > 0 ? parts.join(" && ") : null;
}
