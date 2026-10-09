import { useIncidentActions } from "@/entities/incidents/model";
import React, { useCallback } from "react";
import { Severity } from "@/entities/incidents/model/models";
import { IncidentSeveritySelect } from "./incident-severity-select";
import { useUserPermissions } from "@/shared/lib/hooks/useUserPermissions";
import { showErrorToast } from "@/shared/ui";

type Props = {
  incidentId: string;
  teamId?: string | null;
  value: Severity;
  onChange?: (status: Severity) => void;
  className?: string;
};

export function IncidentChangeSeveritySelect({
  incidentId,
  teamId,
  value,
  onChange,
  className,
}: Props) {
  const { changeSeverity } = useIncidentActions();
  const { can } = useUserPermissions();
  const canChange = can("update:incident", { team_id: teamId });

  const handleChange = useCallback(
    (value: any) => {
      const _asyncUpdate = async (val: any) => {
        await changeSeverity(incidentId, val || null);
        onChange?.(val || null);
      };
      _asyncUpdate(value).catch((error) => showErrorToast(error));
    },
    [incidentId, changeSeverity, onChange]
  );

  return (
    <span title={!canChange ? "Read only: changes are not allowed for your role or team" : undefined}>
    <IncidentSeveritySelect
      disabled={!canChange}
      className={className}
      value={value}
      onChange={handleChange}
    />
    </span>
  );
}
