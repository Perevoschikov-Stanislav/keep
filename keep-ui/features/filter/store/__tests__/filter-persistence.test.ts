import {
  getFilterStorageKey,
  loadSavedFacetsState,
  saveFacetsState,
  clearSavedFacetsState,
  buildCelFromSavedFilters,
} from "../filter-persistence";
import { FacetDto, FacetState } from "../../models";

describe("filter-persistence", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  describe("getFilterStorageKey", () => {
    it("returns correct key for incidents", () => {
      expect(getFilterStorageKey("incidents")).toBe("keep-filters-incidents");
    });

    it("returns correct key for incidents with viewId", () => {
      expect(getFilterStorageKey("incidents", null, "it")).toBe(
        "keep-filters-incidents-it"
      );
      expect(getFilterStorageKey("incidents", null, "ops")).toBe(
        "keep-filters-incidents-ops"
      );
      expect(getFilterStorageKey("incidents-it")).toBe(
        "keep-filters-incidents-it"
      );
      expect(getFilterStorageKey("incidents-ops")).toBe(
        "keep-filters-incidents-ops"
      );
    });

    it("returns correct key for alerts with pathname /alerts/feed", () => {
      expect(getFilterStorageKey("alerts", "/alerts/feed")).toBe(
        "keep-filters-alerts-feed"
      );
    });

    it("returns correct key for alerts with custom preset pathname", () => {
      expect(getFilterStorageKey("alerts", "/alerts/db-errors")).toBe(
        "keep-filters-alerts-db-errors"
      );
    });

    it("defaults to feed preset when pathname has no preset", () => {
      expect(getFilterStorageKey("alerts", "/alerts")).toBe(
        "keep-filters-alerts-feed"
      );
    });

    it("handles default or unknown panelId", () => {
      expect(getFilterStorageKey("")).toBe("keep-filters-default");
    });
  });

  describe("saveFacetsState and loadSavedFacetsState", () => {
    it("saves and loads valid facets state", () => {
      const state: FacetState = {
        Status: { firing: true },
        Severity: { "1": true, "2": true },
      };
      saveFacetsState("test-key", state);
      expect(loadSavedFacetsState("test-key")).toEqual(state);
    });

    it("removes storage item when facets state has no selected values", () => {
      saveFacetsState("test-key", { Status: { firing: true } });
      expect(loadSavedFacetsState("test-key")).not.toBeNull();

      saveFacetsState("test-key", { Status: {}, Severity: {} });
      expect(loadSavedFacetsState("test-key")).toBeNull();
      expect(window.localStorage.getItem("test-key")).toBeNull();
    });

    it("returns null when no data in localStorage", () => {
      expect(loadSavedFacetsState("non-existent-key")).toBeNull();
    });

    it("isolates filter storage between different views", () => {
      const itKey = getFilterStorageKey("incidents", null, "it");
      const opsKey = getFilterStorageKey("incidents", null, "ops");
      expect(itKey).not.toBe(opsKey);

      saveFacetsState(itKey, { Severity: { critical: true } });
      saveFacetsState(opsKey, { Severity: { low: true } });

      expect(loadSavedFacetsState(itKey)).toEqual({
        Severity: { critical: true },
      });
      expect(loadSavedFacetsState(opsKey)).toEqual({
        Severity: { low: true },
      });
    });

    it("returns null on corrupted JSON", () => {
      window.localStorage.setItem("corrupted", "{not-valid-json");
      expect(loadSavedFacetsState("corrupted")).toBeNull();
    });
  });

  describe("clearSavedFacetsState", () => {
    it("removes key from localStorage", () => {
      saveFacetsState("test-key", { Status: { firing: true } });
      expect(loadSavedFacetsState("test-key")).not.toBeNull();

      clearSavedFacetsState("test-key");
      expect(loadSavedFacetsState("test-key")).toBeNull();
    });
  });

  describe("buildCelFromSavedFilters", () => {
    const mockFacets: FacetDto[] = [
      {
        id: "Status",
        name: "Status",
        property_path: "status",
        is_static: true,
        is_lazy: false,
      },
      {
        id: "Severity",
        name: "Severity",
        property_path: "severity",
        is_static: true,
        is_lazy: false,
      },
      {
        id: "cluster-facet-id",
        name: "Cluster",
        property_path: "labels.cluster",
        is_static: false,
        is_lazy: true,
      },
    ];

    it("constructs CEL string with Status and custom facets", () => {
      const saved: FacetState = {
        Status: { firing: true },
        "cluster-facet-id": { "dit-prod": true },
      };

      const cel = buildCelFromSavedFilters(saved, mockFacets);
      expect(cel).toBe("(status in ['firing']) && (labels.cluster in ['dit-prod'])");
    });

    it("falls back to defaultStatuses when Status is not in savedState", () => {
      const saved: FacetState = {
        "cluster-facet-id": { "dit-prod": true },
      };

      const cel = buildCelFromSavedFilters(saved, mockFacets, [
        "firing",
        "acknowledged",
      ]);
      expect(cel).toBe(
        "(status in ['firing', 'acknowledged']) && (labels.cluster in ['dit-prod'])"
      );
    });

    it("matches facet by name if id does not match", () => {
      const saved: FacetState = {
        Status: { firing: true },
        Cluster: { "customer-b-prod": true },
      };

      const cel = buildCelFromSavedFilters(saved, mockFacets);
      expect(cel).toBe(
        "(status in ['firing']) && (labels.cluster in ['customer-b-prod'])"
      );
    });

    it("matches status facet by backend UUID", () => {
      const facetsWithUuid: FacetDto[] = [
        {
          id: "1e7b1d6e-1c2b-4f8e-9f8e-1c2b4f8e9f8e",
          name: "Status",
          property_path: "status",
          is_static: true,
          is_lazy: false,
        },
      ];

      const saved: FacetState = {
        "1e7b1d6e-1c2b-4f8e-9f8e-1c2b4f8e9f8e": { resolved: true },
      };

      const cel = buildCelFromSavedFilters(saved, facetsWithUuid, [
        "firing",
        "acknowledged",
      ]);
      expect(cel).toBe("(status in ['resolved'])");
    });

    it("does not fall back to defaultStatuses when status is explicitly empty in savedState", () => {
      const saved: FacetState = {
        Status: {},
      };

      const cel = buildCelFromSavedFilters(saved, mockFacets, [
        "firing",
        "acknowledged",
      ]);
      expect(cel).toBeNull();
    });
  });
});
