import clsx from "clsx";
import { Status } from "@/entities/incidents/model";
import { STATUS_ICONS } from "@/entities/incidents/ui";
import Select, { ClassNamesConfig } from "react-select";
import { useIncidentActions } from "@/entities/incidents/model";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { capitalize } from "@/utils/helpers";
import { useUserPermissions } from "@/shared/lib/hooks/useUserPermissions";
import { showErrorToast } from "@/shared/ui";

const customClassNames: ClassNamesConfig<any, false, any> = {
  container: () => "inline-flex",
  control: (state) =>
    clsx(
      "p-1 min-w-14 !rounded-full !min-h-0",
      state.isFocused ? "border-orange-500" : ""
    ),
  valueContainer: () => "!p-0",
  dropdownIndicator: () => "!p-0",
  indicatorSeparator: () => "hidden",
  menuList: () => "!p-0",
  menu: () => "!p-0 !overflow-hidden min-w-36",
  option: (state) =>
    clsx(
      "!p-1",
      state.isSelected ? "!bg-orange-500 !text-white [&_svg]:text-white" : "",
      state.isFocused && !state.isSelected ? "!bg-slate-100" : ""
    ),
};

type Props = {
  incidentId: string;
  teamId?: string | null;
  value: Status;
  expectedRevision?: number;
  onChange?: (status: Status) => void;
  className?: string;
};

export function IncidentChangeStatusSelect({
  incidentId,
  teamId,
  value,
  expectedRevision,
  onChange,
  className,
}: Props) {
  // Use a portal to render the menu outside the table container with overflow: hidden
  const menuPortalTarget = useRef<HTMLElement | null>(null);
  const [isDisabled, setIsDisabled] = useState(false);
  useEffect(() => {
    menuPortalTarget.current = document.body;
  }, []);

  const { changeStatus } = useIncidentActions();
  const { can } = useUserPermissions();
  const canChange = can("update:incident", { team_id: teamId });
  const canDelete = can("delete:incident", { team_id: teamId });
  const statusOptions = useMemo(
    () =>
      Object.values(Status)
        .filter((status) => canDelete || ![Status.Deleted, Status.Merged].includes(status) || status === value)
        .map((status) => ({
          value: status,
          label: (
            <div className="flex items-center">
              {STATUS_ICONS[status]}
              <span>{capitalize(status)}</span>
            </div>
          ),
        })),
    [value, canDelete]
  );

  const handleChange = useCallback(
    (option: any) => {
      if (!canChange) return;
      const _asyncUpdate = async (option: any) => {
        setIsDisabled(true);
        try {
          await changeStatus(incidentId, option?.value || null, undefined, expectedRevision);
          onChange?.(option?.value || null);
        } finally {
          setIsDisabled(false);
        }
      };
      _asyncUpdate(option).catch((error) => showErrorToast(error));
    },
    [incidentId, changeStatus, onChange, canChange, expectedRevision]
  );

  const selectedOption = useMemo(
    () => statusOptions.find((option) => option.value === value),
    [statusOptions, value]
  );

  return (
    <span title={!canChange ? "Read only: changes are not allowed for your role or team" : undefined}>
    <Select
      aria-label="Incident status"
      instanceId={`incident-status-select-${incidentId}`}
      className={className}
      isSearchable={false}
      options={statusOptions}
      value={selectedOption}
      onChange={handleChange}
      isDisabled={isDisabled || !canChange}
      menuIsOpen={!canChange ? false : undefined}
      placeholder="Status"
      classNames={customClassNames}
      menuPortalTarget={menuPortalTarget.current}
      menuPosition="fixed"
    />
    </span>
  );
}
