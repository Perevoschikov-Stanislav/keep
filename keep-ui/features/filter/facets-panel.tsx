import React, { useEffect, useMemo, useRef, useState } from "react";
import { Facet } from "./facet";
import {
  FacetDto,
  FacetOptionDto,
  FacetOptionsQueries,
  FacetsConfig,
  UpdateFacetDto,
} from "./models";
import { PlusIcon, XMarkIcon } from "@heroicons/react/24/outline";
import "react-loading-skeleton/dist/skeleton.css";
import clsx from "clsx";
import { FacetStoreProvider, useFacetsConfig, useNewFacetStore } from "./store";
import { useStore } from "zustand";
import { useLocalStorage } from "@/utils/hooks/useLocalStorage";
import { useConfig } from "@/utils/hooks/useConfig";
import { usePathname } from "next/navigation";
import {
  getFilterStorageKey,
  clearSavedFacetsState,
} from "./store/filter-persistence";
import { EditFacetModal } from "./edit-facet-modal";
import {
  DndContext,
  closestCenter,
  PointerSensor,
  useSensor,
  useSensors,
  DragEndEvent,
} from "@dnd-kit/core";
import {
  arrayMove,
  SortableContext,
  verticalListSortingStrategy,
  useSortable,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";

export interface FacetsPanelProps {
  panelId: string;
  className: string;
  facets: FacetDto[] | null | undefined;
  facetOptions: { [key: string]: FacetOptionDto[] };
  areFacetOptionsLoading?: boolean;
  /** Token to clear filters related to facets */
  clearFiltersToken?: string | null;
  /**
   * Object with facets that should be unchecked by default.
   * Key is the facet name, value is the list of option values to uncheck.
   **/
  facetsConfig?: FacetsConfig;
  renderFacetOptionLabel?: (
    facetName: string,
    optionDisplayName: string
  ) => React.JSX.Element | string | undefined;
  renderFacetOptionIcon?: (
    facetName: string,
    optionDisplayName: string
  ) => React.JSX.Element | undefined;
  onCelChange?: (cel: string) => void;
  onAddFacet: () => void;
  onDeleteFacet: (facetId: string) => void;
  onUpdateFacet?: (facetId: string, updatedFacet: UpdateFacetDto) => void;
  onLoadFacetOptions: (facetId: string) => void;
  onReloadFacetOptions: (facetsQuery: FacetOptionsQueries) => void;
}

interface SortableFacetItemProps {
  id: string;
  facet: FacetDto;
  panelId?: string;
  isOpenByDefault?: boolean;
  options?: FacetOptionDto[];
  onLoadOptions?: () => void;
  onDelete?: () => void;
  onEdit?: () => void;
}

const SortableFacetItem: React.FC<SortableFacetItemProps> = ({
  id,
  facet,
  panelId,
  isOpenByDefault,
  options,
  onLoadOptions,
  onDelete,
  onEdit,
}) => {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id });

  const style: React.CSSProperties = {
    transform: CSS.Translate.toString(transform),
    transition,
    opacity: isDragging ? 0.5 : 1,
    position: "relative",
    zIndex: isDragging ? 10 : "auto",
  };

  return (
    <div ref={setNodeRef} style={style}>
      <Facet
        facet={facet}
        panelId={panelId}
        isOpenByDefault={isOpenByDefault}
        options={options}
        dragHandleProps={{ ...attributes, ...listeners }}
        onLoadOptions={onLoadOptions}
        onDelete={onDelete}
        onEdit={onEdit}
      />
    </div>
  );
};

export const FacetsPanel: React.FC<FacetsPanelProps> = ({
  panelId,
  className,
  facets,
  facetOptions,
  areFacetOptionsLoading = false,
  clearFiltersToken,
  facetsConfig,
  onCelChange = undefined,
  onAddFacet = undefined,
  onDeleteFacet = undefined,
  onUpdateFacet = undefined,
  onLoadFacetOptions = undefined,
  onReloadFacetOptions = undefined,
}) => {
  const { data: configData } = useConfig();
  const defaultFacetsOrder = useMemo(() => {
    return (
      configData?.DEFAULT_FACETS_ORDER || ["Zone", "Cluster", "Namespace"]
    );
  }, [configData?.DEFAULT_FACETS_ORDER]);

  const [customOrder, setCustomOrder] = useLocalStorage<string[]>(
    `facets-order-${panelId}`,
    []
  );

  const [editingFacet, setEditingFacet] = useState<FacetDto | null>(null);

  const facetOptionsRef = useRef<Record<string, FacetOptionDto[]>>(facetOptions);
  facetOptionsRef.current = facetOptions;
  const onCelChangeRef = useRef(onCelChange);
  onCelChangeRef.current = onCelChange;
  const onReloadFacetOptionsRef = useRef(onReloadFacetOptions);
  onReloadFacetOptionsRef.current = onReloadFacetOptions;
  const pathname = usePathname();
  const storageKey = useMemo(
    () => getFilterStorageKey(panelId, pathname),
    [panelId, pathname]
  );
  const store = useNewFacetStore(facetsConfig, panelId);
  const facetOptionQueries = useStore(
    store,
    (state) => state.queriesState.facetOptionQueries
  );
  const filterCel = useStore(store, (state) => state.queriesState.filterCel);

  const setAreOptionsReLoading = useStore(
    store,
    (state) => state.setAreOptionsReLoading
  );
  const setFacetOptions = useStore(store, (state) => state.setFacetOptions);
  const setFacets = useStore(store, (state) => state.setFacets);
  const clearFilters = useStore(store, (state) => state.clearFilters);

  useEffect(
    () => setAreOptionsReLoading(areFacetOptionsLoading),
    [areFacetOptionsLoading, setAreOptionsReLoading]
  );
  useEffect(
    () => setFacetOptions(facetOptions),
    [facetOptions, setFacetOptions]
  );
  useEffect(() => {
    if (facets != null) {
      setFacets(facets);
    }
  }, [facets, setFacets]);
  useEffect(() => {
    filterCel !== null && onCelChangeRef.current?.(filterCel);
  }, [filterCel]);
  useEffect(() => {
    facetOptionQueries && onReloadFacetOptionsRef.current?.(facetOptionQueries);
  }, [JSON.stringify(facetOptionQueries)]);

  useEffect(
    function clearFiltersWhenTokenChange(): void {
      if (clearFiltersToken) {
        clearSavedFacetsState(storageKey);
        clearFilters();
      }
    },
    [clearFiltersToken, clearFilters, storageKey]
  );

  // Sort facets based on:
  // 1. User drag & drop order (saved in customOrder localStorage)
  // 2. Explicit facet.order
  // 3. DEFAULT_FACETS_ORDER (e.g. Zone, Cluster, Namespace top-most)
  // 4. Static facets (Severity, Status, Source, Incident, Dismissed)
  // 5. Remaining facets
  const sortedFacets = useMemo(() => {
    if (!facets) return null;

    const items = [...facets];
    return items.sort((a, b) => {
      // 1. Custom order saved by user drag & drop
      if (customOrder && customOrder.length > 0) {
        const indexA = customOrder.indexOf(a.name);
        const indexB = customOrder.indexOf(b.name);
        if (indexA !== -1 && indexB !== -1) return indexA - indexB;
        if (indexA !== -1) return -1;
        if (indexB !== -1) return 1;
      }

      // 2. Explicit order on FacetDto
      if (a.order != null && b.order != null) {
        return a.order - b.order;
      }
      if (a.order != null) return -1;
      if (b.order != null) return 1;

      // 3. Default facets order from config (Zone, Cluster, Namespace)
      const defaultIndexA = defaultFacetsOrder.indexOf(a.name);
      const defaultIndexB = defaultFacetsOrder.indexOf(b.name);
      if (defaultIndexA !== -1 && defaultIndexB !== -1) {
        return defaultIndexA - defaultIndexB;
      }
      if (defaultIndexA !== -1) return -1;
      if (defaultIndexB !== -1) return 1;

      return 0;
    });
  }, [facets, customOrder, defaultFacetsOrder]);

  const sensors = useSensors(
    useSensor(PointerSensor, {
      activationConstraint: {
        distance: 5,
      },
    })
  );

  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    if (over && active.id !== over.id && sortedFacets) {
      const oldIndex = sortedFacets.findIndex((f) => f.id === active.id);
      const newIndex = sortedFacets.findIndex((f) => f.id === over.id);
      if (oldIndex !== -1 && newIndex !== -1) {
        const newItems = arrayMove(sortedFacets, oldIndex, newIndex);
        setCustomOrder(newItems.map((f) => f.name));
      }
    }
  };

  const handleUpdateFacet = (facetId: string, updatedFacet: UpdateFacetDto) => {
    if (editingFacet && updatedFacet.name && updatedFacet.name !== editingFacet.name) {
      // Update custom order if the facet was renamed
      if (customOrder && customOrder.includes(editingFacet.name)) {
        setCustomOrder(
          customOrder.map((name) =>
            name === editingFacet.name ? updatedFacet.name! : name
          )
        );
      }
    }
    onUpdateFacet?.(facetId, updatedFacet);
  };

  return (
    <section
      id={`${panelId}-facets`}
      className={clsx("w-48 lg:w-56", className)}
      data-testid="facets-panel"
    >
      <div className="space-y-2">
        <div className="flex justify-between">
          {/* Facet button */}
          <button
            onClick={() => onAddFacet && onAddFacet()}
            className="p-1 pr-2 text-sm text-gray-600 hover:bg-gray-100 rounded flex items-center gap-1"
          >
            <PlusIcon className="h-4 w-4" />
            Add Facet
          </button>
          <button
            onClick={() => {
              clearSavedFacetsState(storageKey);
              clearFilters();
            }}
            className="p-1 pr-2 text-sm text-gray-600 hover:bg-gray-100 rounded flex items-center gap-1"
          >
            <XMarkIcon className="h-4 w-4" />
            Reset
          </button>
        </div>
        <FacetStoreProvider store={store}>
          {!sortedFacets &&
            [undefined, undefined, undefined].map((_, index) => (
              <Facet
                facet={
                  {
                    id: index.toString(),
                    name: "",
                    is_static: true,
                  } as FacetDto
                }
                key={index}
                panelId={panelId}
                isOpenByDefault={false}
              />
            ))}
          {sortedFacets && (
            <DndContext
              sensors={sensors}
              collisionDetection={closestCenter}
              onDragEnd={handleDragEnd}
            >
              <SortableContext
                items={sortedFacets.map((facet) => facet.id)}
                strategy={verticalListSortingStrategy}
              >
                {sortedFacets.map((facet) => {
                  const isOpenByDefault =
                    facet.is_open_by_default ||
                    facetsConfig?.[facet.id]?.isOpenByDefault ||
                    facetsConfig?.[facet.name]?.isOpenByDefault;

                  return (
                    <SortableFacetItem
                      key={facet.id}
                      id={facet.id}
                      facet={facet}
                      panelId={panelId}
                      isOpenByDefault={isOpenByDefault}
                      options={facetOptions?.[facet.id]}
                      onLoadOptions={() =>
                        onLoadFacetOptions && onLoadFacetOptions(facet.id)
                      }
                      onDelete={() => onDeleteFacet && onDeleteFacet(facet.id)}
                      onEdit={() => setEditingFacet(facet)}
                    />
                  );
                })}
              </SortableContext>
            </DndContext>
          )}
        </FacetStoreProvider>
      </div>

      {editingFacet && (
        <EditFacetModal
          facet={editingFacet}
          isOpen={!!editingFacet}
          onClose={() => setEditingFacet(null)}
          onUpdateFacet={handleUpdateFacet}
        />
      )}
    </section>
  );
};
