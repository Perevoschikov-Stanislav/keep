"use client";

import {
  useIncidentActions,
  type IncidentDto,
} from "@/entities/incidents/model";
import { Badge, Button, Icon, Subtitle } from "@tremor/react";
import { Link } from "@/components/ui";
import { ArrowRightIcon } from "@heroicons/react/16/solid";
import { MdBlock, MdDone, MdModeEdit, MdPlayArrow } from "react-icons/md";
import React, { useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { ManualRunWorkflowModal } from "@/features/workflows/manual-run-workflow";
import { CreateOrUpdateIncidentForm } from "features/incidents/create-or-update-incident";
import Modal from "@/components/ui/Modal";
import { getIncidentName } from "@/entities/incidents/lib/utils";
import { useIncident } from "@/utils/hooks/useIncidents";
import { IncidentOverview } from "./incident-overview";
import { TbInfoCircle, TbTopologyStar3 } from "react-icons/tb";
import { useConfig } from "@/utils/hooks/useConfig";
import { TicketingIncidentOptions } from "./ticketing-incident-options";
import { useUserPermissions } from "@/shared/lib/hooks/useUserPermissions";
import { SilenceBadge } from "@/features/silences/silence-badge";
import { SilenceModal, UnsilenceModal } from "@/features/silences/silence-modal";
import { IoNotificationsOffOutline } from "react-icons/io5";

export function IncidentHeader({
  incident: initialIncidentData,
}: {
  incident: IncidentDto;
}) {
  const { data: fetchedIncident } = useIncident(initialIncidentData.id, {
    fallbackData: initialIncidentData,
    revalidateOnMount: false,
  });
  const { deleteIncident, confirmPredictedIncident } = useIncidentActions();
  const incident = fetchedIncident || initialIncidentData;
  const { can } = useUserPermissions();
  const { data: config } = useConfig();

  const router = useRouter();
  const pathname = usePathname();

  const [isFormOpen, setIsFormOpen] = useState<boolean>(false);
  const [isSilenceModalOpen, setIsSilenceModalOpen] = useState<boolean>(false);
  const [isUnsilenceModalOpen, setIsUnsilenceModalOpen] = useState<boolean>(false);

  const [runWorkflowModalIncident, setRunWorkflowModalIncident] =
    useState<IncidentDto | null>();

  const handleCloseForm = () => {
    setIsFormOpen(false);
  };

  const handleFinishEdit = () => {
    setIsFormOpen(false);
  };
  const handleRunWorkflow = () => {
    setRunWorkflowModalIncident(incident);
  };

  const handleStartEdit = () => {
    setIsFormOpen(true);
  };

  const pathNameCapitalized = pathname
    .split("/")
    .pop()
    ?.replace(/^[a-z]/, (match) => match.toUpperCase());

  return (
    <>
      <header className="flex flex-col mb-1">
        <div className="flex flex-row justify-between items-end mb-2.5">
          <div>
            <Subtitle className="text-sm">
              <Link href="/incidents">All Incidents</Link>{" "}
              <Icon icon={ArrowRightIcon} color="gray" size="xs" />{" "}
              {incident.is_candidate ? "Possible " : ""}
              {getIncidentName(incident)}
              {pathNameCapitalized && (
                <>
                  <Icon icon={ArrowRightIcon} color="gray" size="xs" />
                  {pathNameCapitalized}
                </>
              )}
            </Subtitle>
          </div>

          {!incident.is_candidate && (
            <div className="flex">
              {config?.KEEP_TICKETING_ENABLED && (
                <TicketingIncidentOptions
                  incident={incident}
                />
              )}
              <Button
                color="orange"
                size="xs"
                variant="secondary"
                className="!py-0.5 mr-2"
                icon={MdPlayArrow}
                disabled={!can("execute:workflows", incident)}
                onClick={(e: React.MouseEvent) => {
                  e.preventDefault();
                  e.stopPropagation();
                  handleRunWorkflow();
                }}
              >
                Run Workflow
              </Button>
              <Button
                color="orange"
                size="xs"
                variant="secondary"
                className="!py-0.5"
                icon={MdModeEdit}
                disabled={!can("update:incident", incident)}
                title={!can("update:incident", incident) ? "Read only: changes are not allowed for your role or team" : undefined}
                onClick={(e: React.MouseEvent) => {
                  e.preventDefault();
                  e.stopPropagation();
                  handleStartEdit();
                }}
              >
                Edit Incident
              </Button>
              {!!incident.silence?.reasons?.length && (
                <Button
                  color="orange"
                  size="xs"
                  variant="secondary"
                  className="!py-0.5 ml-2"
                  icon={IoNotificationsOffOutline}
                  disabled={!can("update:silence", incident)}
                  title={!can("update:silence", incident) ? "Read only: no update:silence permission for this team" : undefined}
                  onClick={(e: React.MouseEvent) => {
                    e.preventDefault();
                    e.stopPropagation();
                    setIsUnsilenceModalOpen(true);
                  }}
                >
                  Unsilence
                </Button>
              )}
              {!incident.silence?.silenced && (
                <Button
                  color="orange"
                  size="xs"
                  variant="secondary"
                  className="!py-0.5 ml-2"
                  icon={IoNotificationsOffOutline}
                  disabled={!can("write:silence", incident)}
                  title={!can("write:silence", incident) ? "Read only: no write:silence permission for this team" : undefined}
                  onClick={(e: React.MouseEvent) => {
                    e.preventDefault();
                    e.stopPropagation();
                    setIsSilenceModalOpen(true);
                  }}
                >
                  Silence
                </Button>
              )}
            </div>
          )}
        </div>
        <div className="flex justify-start items-center text-sm gap-2">
          <div className="prose-2xl flex-grow flex gap-1 items-center">
            {incident.incident_type == "topology" && (
              <Badge
                color="blue"
                size="xs"
                icon={TbTopologyStar3}
                tooltip="Created by topology correlation"
              >
                Topology
              </Badge>
            )}
            {incident.rule_is_deleted && (
              <Badge
                color="orange"
                size="xs"
                icon={TbInfoCircle}
                tooltip={`Created by deleted rule ${incident.rule_name}`}
              >
                Orphaned
              </Badge>
            )}
            <SilenceBadge metadata={incident.silence} isIncident={true} />
          </div>
          {incident.is_candidate && (
            <div className="space-x-1 flex flex-row items-center justify-center">
              <Button
                color="orange"
                size="xs"
                tooltip="Confirm incident"
                variant="secondary"
                title="Confirm"
                icon={MdDone}
                disabled={!can("write:incident", incident)}
                onClick={(e: React.MouseEvent) => {
                  e.preventDefault();
                  e.stopPropagation();
                  confirmPredictedIncident(incident.id!);
                }}
              >
                Confirm
              </Button>
              <Button
                color="red"
                size="xs"
                variant="secondary"
                tooltip={"Discard"}
                icon={MdBlock}
                disabled={!can("delete:incident", incident)}
                onClick={async (e: React.MouseEvent) => {
                  e.preventDefault();
                  e.stopPropagation();
                  const success = await deleteIncident(incident.id);
                  if (success) {
                    router.push("/incidents");
                  }
                }}
              />
            </div>
          )}
        </div>
      </header>
      <IncidentOverview incident={incident} />
      <Modal
        isOpen={isFormOpen}
        onClose={handleCloseForm}
        className="w-[600px]"
        title="Edit Incident"
      >
        <CreateOrUpdateIncidentForm
          incidentToEdit={incident}
          exitCallback={handleFinishEdit}
        />
      </Modal>
      <ManualRunWorkflowModal
        incident={runWorkflowModalIncident}
        onClose={() => setRunWorkflowModalIncident(null)}
      />
      {isSilenceModalOpen && (
        <SilenceModal
          isOpen={true}
          onClose={() => setIsSilenceModalOpen(false)}
          incident={incident}
          onSuccess={() => setIsSilenceModalOpen(false)}
        />
      )}
      {isUnsilenceModalOpen && incident.silence?.reasons && (
        <UnsilenceModal
          isOpen={true}
          onClose={() => setIsUnsilenceModalOpen(false)}
          targetName={getIncidentName(incident)}
          teamId={incident.team_id ?? null}
          reasons={incident.silence.reasons}
          onSuccess={() => setIsUnsilenceModalOpen(false)}
        />
      )}
    </>
  );
}
