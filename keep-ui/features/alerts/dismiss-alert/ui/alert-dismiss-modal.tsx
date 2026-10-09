import React from "react";
import { AlertDto } from "@/entities/alerts/model";
import { SilenceModal, UnsilenceModal } from "@/features/silences/silence-modal";
import { useAlerts } from "@/entities/alerts/model/useAlerts";
import { useRevalidateMultiple } from "@/shared/lib/state-utils";
import { useApi } from "@/shared/lib/hooks/useApi";
import { toast } from "react-toastify";
import { showErrorToast } from "@/shared/ui";
import Modal from "@/components/ui/Modal";
import { Button, Subtitle } from "@tremor/react";
import { useUserPermissions } from "@/shared/lib/hooks/useUserPermissions";

interface Props {
  preset?: string;
  alert: AlertDto[] | null | undefined;
  handleClose: () => void;
}

export function AlertDismissModal({
  alert: alerts,
  handleClose,
}: Props) {
  const { alertsMutator } = useAlerts();
  const revalidateMultiple = useRevalidateMultiple();
  const api = useApi();
  const { can } = useUserPermissions();
  const [restoring, setRestoring] = React.useState(false);

  if (!alerts || alerts.length === 0) return null;

  const firstAlert = alerts[0];
  const isSilenced = !!firstAlert.silence?.silenced;
  const isLegacyDismissed = !isSilenced && !!firstAlert.dismissed;

  // If already silenced with structured silence reasons, show selective UnsilenceModal
  if (alerts.length === 1 && isSilenced && firstAlert.silence?.reasons && firstAlert.silence.reasons.length > 0) {
    return (
      <UnsilenceModal
        isOpen={true}
        onClose={handleClose}
        targetName={firstAlert.name || firstAlert.fingerprint}
        teamId={firstAlert.team_id ?? null}
        reasons={firstAlert.silence.reasons}
        onSuccess={async () => {
          await alertsMutator();
          await revalidateMultiple(["/preset", "/silences"]);
          handleClose();
        }}
      />
    );
  }

  // If legacy dismissed (without structured silence reasons), show restore confirmation
  if (alerts.length === 1 && isLegacyDismissed) {
    const handleLegacyRestore = async () => {
      if (restoring || !can("update:silence", firstAlert)) return;
      setRestoring(true);
      try {
        await api.post("/alerts/batch_enrich?dispose_on_new_alert=false", {
          enrichments: {
            dismissed: false,
            dismissUntil: "",
            note: "Restored via UI",
          },
          fingerprints: alerts.map((a) => a.fingerprint),
        });
        toast.success("Alert restored successfully");
        await alertsMutator();
        await revalidateMultiple(["/preset", "/silences"]);
        handleClose();
      } catch (err) {
        showErrorToast(err, "Failed to restore alert");
      } finally {
        setRestoring(false);
      }
    };

    return (
      <Modal
        isOpen={true}
        onClose={handleClose}
        title="Restore Alert"
        className="w-full max-w-sm p-6"
      >
        <div className="space-y-4">
          <Subtitle className="text-center text-xs">
            Are you sure you want to restore this legacy dismissed alert?
          </Subtitle>
          <div className="flex justify-center gap-2">
            <Button size="xs" variant="secondary" color="gray" onClick={handleClose}>
              Cancel
            </Button>
            <Button size="xs" color="orange" onClick={handleLegacyRestore}
              disabled={restoring || !can("update:silence", firstAlert)}>
              Confirm Restore
            </Button>
          </div>
        </div>
      </Modal>
    );
  }

  // Otherwise, create a new silence rule using the unified SilenceModal
  return (
    <SilenceModal
      isOpen={true}
      onClose={handleClose}
      alerts={alerts}
      onSuccess={async () => {
        await alertsMutator();
        await revalidateMultiple(["/preset", "/silences"]);
        handleClose();
      }}
    />
  );
}
