import { useEffect, useMemo, useRef, useState } from "react";
import { Title } from "@tremor/react";
import { ChevronDownIcon, ChevronRightIcon } from "@heroicons/react/20/solid";
import { useLocalStorage } from "utils/hooks/useLocalStorage";
import { usePathname } from "next/navigation";
import Skeleton from "react-loading-skeleton";
import { FacetValue } from "./facet-value";
import { FacetDto, FacetOptionDto } from "./models";
import { TrashIcon, PencilIcon } from "@heroicons/react/24/outline";
import { Bars2Icon } from "@heroicons/react/20/solid";
import { useExistingFacetsPanelStore } from "./store";
import { isLazyFacet, stringToValue, valueToString } from "./store/utils";

export interface FacetProps {
  facet: FacetDto;
  panelId?: string;
  isOpenByDefault?: boolean;
  options?: FacetOptionDto[];
  showIcon?: boolean;
  dragHandleProps?: Record<string, any>;
  onLoadOptions?: () => void;
  onDelete?: () => void;
  onEdit?: () => void;
}

export const Facet: React.FC<FacetProps> = ({
  facet,
  panelId,
  isOpenByDefault,
  options,
  showIcon = true,
  dragHandleProps,
  onLoadOptions,
  onDelete,
  onEdit,
}) => {
  const pathname = usePathname();
  // Get preset name from URL
  const presetName = pathname?.split("/").pop() || "default";

  // Lazy facets (high-cardinality user-defined facets) start collapsed and only
  // fetch their options when the user expands them. Static facets (severity,
  // status, source, ...) always render eagerly. Eagerly mounting and loading
  // options for every lazy facet is what froze the alerts page when many (200+)
  // facets existed (see issue #6577).
  const isLazy = isLazyFacet(facet);

  // Store open/close state in localStorage with a unique key per panel and facet
  const storageKey = `facet-open-${panelId || "default"}-${facet.id}`;
  const [savedIsOpen, setSavedIsOpen] = useLocalStorage<boolean | null>(
    storageKey,
    null
  );

  const shouldBeOpen =
    savedIsOpen !== null && savedIsOpen !== undefined
      ? savedIsOpen
      : (isOpenByDefault ?? facet.is_open_by_default ?? false);

  const [isOpen, setIsOpen] = useState<boolean>(shouldBeOpen);
  const [isLoaded, setIsLoaded] = useState<boolean>(!!options?.length);
  const [isLoading, setIsLoading] = useState<boolean>(false);

  // Sync isOpen when savedIsOpen changes (hydration or storage event)
  useEffect(() => {
    if (savedIsOpen !== null && savedIsOpen !== undefined) {
      setIsOpen(savedIsOpen);
    }
  }, [savedIsOpen]);

  const optionsRef = useRef(options);
  optionsRef.current = options;
  const facetRef = useRef(facet);
  facetRef.current = facet;

  // Subscribe only to this facet's slice of the loading/selection state.
  // Previously every Facet subscribed to the whole `facetOptionsLoadingState`
  // and `facetsState` objects, so toggling one option re-rendered all facets.
  const facetLoadingState = useExistingFacetsPanelStore(
    (state) => state.facetOptionsLoadingState[facet.id]
  );
  const hasLoadingState = useExistingFacetsPanelStore(
    (state) => Object.keys(state.facetOptionsLoadingState).length > 0
  );
  const toggleFacetOption = useExistingFacetsPanelStore(
    (state) => state.toggleFacetOption
  );
  const selectOneFacetOption = useExistingFacetsPanelStore(
    (state) => state.selectOneFacetOption
  );
  const selectAllFacetOptions = useExistingFacetsPanelStore(
    (state) => state.selectAllFacetOptions
  );
  const setFacetActive = useExistingFacetsPanelStore(
    (state) => state.setFacetActive
  );
  const facetState: Record<string, boolean> = useExistingFacetsPanelStore(
    (state) => state.facetsState?.[facet.id]
  );

  const facetConfig = useExistingFacetsPanelStore(
    (state) => state.facetsConfig?.[facet.id]
  );

  const isFacetActive = useExistingFacetsPanelStore(
    (state) => !!state.activeFacetIds?.[facet.id]
  );

  const facetStateRef = useRef(facetState);
  facetStateRef.current = facetState;

  // Auto-open only if marked isOpenByDefault and user has not saved any preference yet
  const didAutoOpenRef = useRef(false);
  useEffect(() => {
    if (
      !didAutoOpenRef.current &&
      savedIsOpen === null &&
      facetConfig?.isOpenByDefault
    ) {
      didAutoOpenRef.current = true;
      setIsOpen(true);
      setSavedIsOpen(true);
    }
  }, [savedIsOpen, facetConfig?.isOpenByDefault, setSavedIsOpen]);

  // When facet is open, ensure it's marked active and trigger option loading if not already loaded
  useEffect(() => {
    if (isOpen) {
      setFacetActive(facet.id);
      if (!isLoaded && !isLoading && (!options || !options.length)) {
        onLoadOptions && onLoadOptions();
        setIsLoading(true);
      }
    }
  }, [isOpen, isLoaded, isLoading, options, facet.id, onLoadOptions, setFacetActive]);

  function getSelectedValues(): string[] {
    return Object.keys(facetStateRef.current || {});
  }

  /** This variable stores placeholders for facet options that are selected, but don't exist.
   * For example, if user selects "foo" and "bar" options, but only "foo" exists in the options list,
   * then "bar" will be added to the options list as a placeholder with 0 matches_count and will be displayed as selected for user.
   * But upon unselection, the option will disappear from the list.
   * Such behavior might happen in case when query params contained options that are not present in the current options list.
   */
  const placeholderOptions: Record<string, FacetOptionDto> = useMemo(() => {
    if (!options) {
      return {};
    }

    if (!facetState) {
      return {};
    }

    const existingOptions = new Set<string>(
      options.map((option) => valueToString(option.value))
    );

    return Object.keys(facetState)
      .filter((value) => !existingOptions.has(value))
      .map((key) => ({
        display_name: stringToValue(key),
        matches_count: 0,
        value: stringToValue(key),
      }))
      .reduce(
        (acc, current) => ({ ...acc, [current.value]: current }),
        {} as Record<string, FacetOptionDto>
      );
  }, [options, facetState]);

  const extendedOptions = useMemo(() => {
    if (!options) {
      return null;
    }

    return [...options, ...Object.values(placeholderOptions)];
  }, [options, placeholderOptions]);

  useEffect(() => {
    setIsLoaded(!!options); // Sync prop change with state

    if (isLoading && options) {
      setIsLoading(false);
    }
    // disabling as the effect has to only run on options change"
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [options]);

  // Store filter value in localStorage per preset and facet
  const [filter, setFilter] = useLocalStorage<string>(
    `facet-${presetName}-${facet.id}-filter`,
    ""
  );

  const isOptionSelected = (optionValue: string) => {
    if (!facetState) {
      if (facetConfig?.checkedByDefaultOptionValues) {
        return facetConfig.checkedByDefaultOptionValues
          .map(valueToString)
          .includes(valueToString(optionValue));
      }
      return true;
    }

    const strValue = valueToString(optionValue);
    return !!facetState[strValue];
  };

  const isOptionSelectable = (facetOption: FacetOptionDto) => {
    return facetOption.matches_count > 0 || !!facetConfig?.canHitEmptyState;
  };

  const handleExpandCollapse = (currentIsOpen: boolean) => {
    const willOpen = !currentIsOpen;
    setIsOpen(willOpen);
    setSavedIsOpen(willOpen);

    // Mark the facet active when expanding so its options get loaded. Lazy
    // facets are inactive until expanded (#6577).
    if (willOpen) {
      setFacetActive(facet.id);
    }

    if (!isLoaded && !isLoading) {
      onLoadOptions && onLoadOptions();
      setIsLoading(true);
    }
  };

  function checkIfOptionExclusievlySelected(optionValue: string): boolean {
    if (!facetState) {
      return false;
    }

    return (
      getSelectedValues().length === 1 && facetState[valueToString(optionValue)]
    );
  }

  const Icon = isOpen ? ChevronDownIcon : ChevronRightIcon;

  function renderSkeleton(key: string) {
    return (
      <div className="flex h-7 items-center px-2 py-1 gap-2" key={key}>
        <Skeleton containerClassName="h-4 w-4" />
        <Skeleton containerClassName="h-4 flex-1" />
      </div>
    );
  }

  function renderFacetValue(facetOption: FacetOptionDto, index: number) {
    return (
      <FacetValue
        key={facetOption.display_name + index}
        label={facetOption.display_name}
        count={facetOption.matches_count}
        showIcon={showIcon}
        isExclusivelySelected={checkIfOptionExclusievlySelected(
          facetOption.value
        )}
        isSelected={
          !!placeholderOptions[facetOption.value] ||
          (isOptionSelected(facetOption.value) &&
            isOptionSelectable(facetOption))
        }
        isSelectable={isOptionSelectable(facetOption)}
        renderLabel={
          facetConfig?.renderOptionLabel
            ? () => facetConfig.renderOptionLabel!(facetOption)
            : () => facetOption.display_name
        }
        renderIcon={
          facetConfig?.renderOptionIcon
            ? () => facetConfig.renderOptionIcon!(facetOption)
            : undefined
        }
        onToggleOption={() => toggleFacetOption(facet.id, facetOption.value)}
        onSelectOneOption={() =>
          selectOneFacetOption(facet.id, facetOption.value)
        }
        onSelectAllOptions={() => selectAllFacetOptions(facet.id)}
      />
    );
  }

  function renderBody() {
    if (facetLoadingState === "loading" || !hasLoadingState) {
      return Array.from({ length: 3 }).map((_, index) =>
        renderSkeleton(`skeleton-${index}`)
      );
    }

    let optionsToRender =
      extendedOptions
        ?.filter((facetOption) =>
          facetOption.display_name
            .toLocaleLowerCase()
            .includes(filter.toLocaleLowerCase())
        )
        .sort((fst, scd) => scd.matches_count - fst.matches_count) || [];

    if (facetConfig?.sortCallback) {
      const sortCallback = facetConfig.sortCallback as any;
      optionsToRender = optionsToRender.sort((fst, scd) =>
        sortCallback(scd) > sortCallback(fst) ? 1 : -1
      );
    }

    if (!optionsToRender.length) {
      return (
        <div className="px-2 py-1 text-sm text-gray-500 italic">
          No matching values found
        </div>
      );
    }

    return optionsToRender.map((facetOption, index) =>
      renderFacetValue(facetOption, index)
    );
  }

  return (
    <div data-testid="facet" className="pb-2 border-b border-gray-200">
      <div
        className="relative flex items-center justify-between px-2 py-2 cursor-pointer hover:bg-gray-50 group"
        onClick={() => handleExpandCollapse(isOpen)}
      >
        <div className="flex items-center space-x-1.5 flex-1 min-w-0 pr-14">
          {dragHandleProps && (
            <span
              {...dragHandleProps}
              onClick={(e) => {
                e.stopPropagation();
              }}
              className="cursor-grab active:cursor-grabbing text-gray-400 hover:text-gray-600 p-0.5"
              title="Drag to reorder"
            >
              <Bars2Icon className="h-4 w-4" />
            </span>
          )}
          <Icon className="size-5 -m-0.5 text-gray-600 flex-shrink-0" />
          {isLoading && <Skeleton containerClassName="h-4 w-20" />}
          {!isLoading && <Title className="text-sm truncate">{facet.name}</Title>}
        </div>
        {!facet.is_static && (
          <div className="absolute right-2 top-2 flex items-center space-x-0.5">
            <button
              data-testid="edit-facet"
              title="Edit facet"
              onClick={(mouseEvent) => {
                mouseEvent.preventDefault();
                mouseEvent.stopPropagation();
                onEdit && onEdit();
              }}
              className="p-1 text-gray-400 hover:text-gray-600"
            >
              <PencilIcon className="h-4 w-4" />
            </button>
            <button
              data-testid="delete-facet"
              title="Delete facet"
              onClick={(mouseEvent) => {
                mouseEvent.preventDefault();
                mouseEvent.stopPropagation();
                onDelete && onDelete();
              }}
              className="p-1 text-gray-400 hover:text-red-600"
            >
              <TrashIcon className="h-4 w-4" />
            </button>
          </div>
        )}
      </div>

      {isOpen && (
        <div>
          {options && options.length >= 10 && (
            <div className="px-2 mb-1">
              <input
                type="text"
                placeholder="Filter values..."
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                className="w-full px-2 py-1 text-sm border border-gray-300 rounded"
              />
            </div>
          )}
          <div
            className={`max-h-60 overflow-y-auto${facetLoadingState === "reloading" ? " pointer-events-none opacity-70" : ""}`}
          >
            {renderBody()}
          </div>
        </div>
      )}
    </div>
  );
};
