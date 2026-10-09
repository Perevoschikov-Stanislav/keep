"use client";

import { useRef, useState } from "react";
import { useSearchParams, usePathname, useRouter } from "next/navigation";
import { Button, Callout } from "@tremor/react";
import { IncidentDto } from "@/entities/incidents/model";
import { useApi } from "@/shared/lib/hooks/useApi";
import { useUserPermissions } from "@/shared/lib/hooks/useUserPermissions";
import { useHydratedSession } from "@/shared/lib/hooks/useHydratedSession";
import { SilenceModal } from "@/features/silences/silence-modal/ui/SilenceModal";
import { showErrorToast } from "@/shared/ui";

const labels = { ack: "Acknowledge", resolve: "Resolve", assign: "Assign to me", silence: "Create silence" };

export function NotificationCommand({ incident, onSuccess }: { incident: IncidentDto; onSuccess: () => void }) {
  const search = useSearchParams();
  const pathname = usePathname();
  const router = useRouter();
  const api = useApi();
  const { can } = useUserPermissions();
  const { data: session } = useHydratedSession();
  const [loading, setLoading] = useState(false);
  const [silenceOpen, setSilenceOpen] = useState(false);
  const pending = useRef<{ signature: string; id: string } | null>(null);
  const raw = search.get("command");
  if (!raw || !Object.keys(labels).includes(raw) || search.getAll("command").length !== 1) return null;
  const command = raw as keyof typeof labels;
  const revisionText = search.get("revision") || "";
  const revision = /^\d+$/.test(revisionText) ? Number(revisionText) : NaN;
  const stale = search.getAll("revision").length !== 1 || !Number.isSafeInteger(revision) ||
    revision !== (incident.lifecycle?.revision ?? 0);
  const permitted = can(command === "silence" ? "write:silence" : "update:incident", incident);
  const email = session?.user?.email;

  const close = () => {
    const next = new URLSearchParams(search.toString());
    next.delete("command");
    next.delete("revision");
    router.replace(pathname + (next.size ? "?" + next.toString() : ""));
  };
  const confirm = async () => {
    if (stale || !permitted || loading) return;
    if (command === "silence") { setSilenceOpen(true); return; }
    if (command === "assign" && !email) return;
    const signature = JSON.stringify([incident.id, revision, command, email]);
    if (pending.current?.signature !== signature) pending.current = { signature, id: crypto.randomUUID() };
    setLoading(true);
    try {
      await api.post(`/incidents/${incident.id}/commands`, {
        schema_version: 1, client_request_id: pending.current.id, incident_id: incident.id,
        expected_revision: revision, command, correlation_id: null,
        ...(command === "assign" ? { assignee: email } : {}),
      });
      onSuccess();
      close();
    } catch (error) {
      showErrorToast(error, "Incident action failed. Refresh the incident before trying again.");
    } finally { setLoading(false); }
  };

  return <>
    <div className="mb-4">
      <Callout title="Confirm notification action" color={stale || !permitted ? "gray" : "blue"}>
        {stale ? "This notification is outdated. Review the current incident and use its controls." :
          !permitted ? "Your role or team cannot perform this action." : `${labels[command]} for this incident?`}
      </Callout>
      <div className="mt-3 flex gap-2">
        <Button onClick={confirm} loading={loading} disabled={stale || !permitted || (command === "assign" && !email)}>
          {labels[command]}
        </Button>
        <Button variant="secondary" onClick={close} disabled={loading}>Cancel</Button>
      </div>
    </div>
    <SilenceModal isOpen={silenceOpen} incident={incident} onClose={() => setSilenceOpen(false)}
      onSuccess={() => { setSilenceOpen(false); onSuccess(); close(); }} />
  </>;
}
