import React from "react";

export type FacetState = Record<string, any | null>;

export interface FacetConfig {
  canHitEmptyState?: boolean;
  checkedByDefaultOptionValues?: string[];
  renderOptionIcon?: (facetOption: FacetOptionDto) => React.JSX.Element | undefined;
  renderOptionLabel?: (
    facetOption: FacetOptionDto
  ) => React.JSX.Element | string | undefined;
  sortCallback?: (facetOption: FacetOptionDto) => number;
  isOpenByDefault?: boolean;
  order?: number;
}

export interface FacetsConfig {
  [facetName: string]: FacetConfig;
}

export interface FacetOptionDto {
  display_name: string;
  value: any;
  matches_count: number;
}

export type FacetOptionsDict = { [facetId: string]: FacetOptionDto[] };
export type FacetOptionsQuery = {
  cel?: string | undefined;
  facet_queries?: FacetOptionsQueries;
};
export type FacetOptionsQueries = { [facet_id: string]: string };

export interface FacetDto {
  id: string;
  property_path: string;
  name: string;
  is_static: boolean;
  is_lazy: boolean;
  order?: number;
  is_open_by_default?: boolean;
}

export interface CreateFacetDto {
  property_path: string;
  name: string;
  order?: number;
  is_open_by_default?: boolean;
}

export interface UpdateFacetDto {
  name?: string;
  property_path?: string;
  order?: number;
  is_open_by_default?: boolean;
}

