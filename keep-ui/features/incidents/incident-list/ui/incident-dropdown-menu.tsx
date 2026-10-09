import { PencilIcon, PlayIcon, TrashIcon } from "@heroicons/react/24/outline";
import { EllipsisHorizontalIcon } from "@heroicons/react/20/solid";
import { IoNotificationsOffOutline } from "react-icons/io5";
import { DropdownMenu } from "@/shared/ui";
import { IncidentDto } from "@/entities/incidents/model";
import { useIncidentActions } from "@/entities/incidents/model/useIncidentActions";
import { useUserPermissions } from "@/shared/lib/hooks/useUserPermissions";

interface Props {
  incident: IncidentDto;
  handleEdit: (incident: IncidentDto) => void;
  handleRunWorkflow: (incident: IncidentDto) => void;
  handleSilence?: (incident: IncidentDto) => void;
  handleUnsilence?: (incident: IncidentDto) => void;
}

export function IncidentDropdownMenu({
  incident,
  handleEdit,
  handleRunWorkflow,
  handleSilence,
  handleUnsilence,
}: Props) {
  const { deleteIncident } = useIncidentActions();
  const { can } = useUserPermissions();

  const isSilenced = !!incident.silence?.silenced;
  const hasSilenceRules = !!incident.silence?.reasons?.length;
  const canSilence = can("write:silence", incident);
  const canUnsilence = can("update:silence", incident);

  return (
    <>
      <DropdownMenu.Menu icon={EllipsisHorizontalIcon} label="">
        <DropdownMenu.Item
          icon={PencilIcon}
          label="Edit"
          disabled={!can("update:incident", incident)}
          title={!can("update:incident", incident) ? "Read only: changes are not allowed" : undefined}
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            handleEdit(incident);
          }}
        />
        <DropdownMenu.Item
          icon={PlayIcon}
          label="Run workflow"
          disabled={!can("execute:workflows", incident)}
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            handleRunWorkflow(incident);
          }}
        />
        {hasSilenceRules && (
          <DropdownMenu.Item
            icon={IoNotificationsOffOutline}
            label="Unsilence"
            disabled={!canUnsilence}
            title={!canUnsilence ? "Read only: no update:silence permission for this team" : undefined}
            onClick={(e) => {
              e.preventDefault();
              e.stopPropagation();
              handleUnsilence?.(incident);
            }}
          />
        )}
        {!isSilenced && (
          <DropdownMenu.Item
            icon={IoNotificationsOffOutline}
            label="Silence"
            disabled={!canSilence}
            title={!canSilence ? "Read only: no write:silence permission for this team" : undefined}
            onClick={(e) => {
              e.preventDefault();
              e.stopPropagation();
              handleSilence?.(incident);
            }}
          />
        )}
        <DropdownMenu.Item
          icon={TrashIcon}
          label="Delete"
          disabled={!can("delete:incident", incident)}
          variant="destructive"
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            deleteIncident(incident.id);
          }}
        />
      </DropdownMenu.Menu>
    </>
  );
}
