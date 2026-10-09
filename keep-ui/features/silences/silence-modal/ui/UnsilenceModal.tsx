import React, { useState } from "react";
import {
  Button,
  Subtitle,
  Callout,
  Badge,
} from "@tremor/react";
import Modal from "@/components/ui/Modal";
import { SilenceReason, useSilenceActions } from "@/entities/silences/model";
import { useUserPermissions } from "@/shared/lib/hooks/useUserPermissions";
import { IoWarningOutline } from "react-icons/io5";

interface UnsilenceModalProps {
  isOpen: boolean;
  onClose: () => void;
  targetName?: string;
  teamId: string | null;
  reasons: SilenceReason[];
  onSuccess?: () => void;
}

export function UnsilenceModal({
  isOpen,
  onClose,
  targetName,
  teamId,
  reasons: matchedReasons,
  onSuccess,
}: UnsilenceModalProps) {
  const { cancelSilence } = useSilenceActions();
  const { can } = useUserPermissions();
  const reasons = React.useMemo(() => Array.from(new Map(
    matchedReasons.map((reason) => [reason.silence_id, reason])
  ).values()), [matchedReasons]);
  const editableReasons = reasons.filter((reason) => !reason.read_only);

  const [selectedSilenceId, setSelectedSilenceId] = useState<string>(
    editableReasons[0]?.silence_id || ""
  );
  const [cancelReason, setCancelReason] = useState<string>("Manual unsilence via UI");
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  // Sync selected ID when reasons change
  React.useEffect(() => {
    if (!editableReasons.some((r) => r.silence_id === selectedSilenceId)) {
      setSelectedSilenceId(editableReasons[0]?.silence_id || "");
    }
    setErrorMsg(null);
  }, [reasons, isOpen]);

  const selectedReason = editableReasons.find((r) => r.silence_id === selectedSilenceId);

  const handleCancel = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedReason || !can("update:silence", { team_id: teamId }) || isLoading) return;

    setIsLoading(true);
    setErrorMsg(null);

    try {
      await cancelSilence(
        selectedReason.silence_id,
        selectedReason.revision,
        cancelReason.trim() || "Manual unsilence via UI"
      );
      onSuccess?.();
      onClose();
    } catch (err: any) {
      const detail = err?.responseJson?.detail ?? err?.response?.data?.detail;
      setErrorMsg(detail?.code === "revision_conflict"
        ? "This rule changed. Close and reopen this dialog to review its current version."
        : detail?.message || err?.message || "Failed to cancel silence");
    } finally {
      setIsLoading(false);
    }
  };

  const hasUpdatePermission = can("update:silence", { team_id: teamId });

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      className="w-full max-w-lg p-6 bg-white dark:bg-gray-800 rounded-lg shadow-xl"
      title={`Unsilence: ${targetName || "Target"}`}
    >
      <form onSubmit={handleCancel} className="space-y-4">
        <Subtitle className="text-xs text-gray-600 dark:text-gray-300">
          This object is currently suppressed by active silence rule(s). Select which silence rule to cancel:
        </Subtitle>

        {reasons.length > 1 && (
          <Callout
            icon={IoWarningOutline}
            color="amber"
            title="Multiple Active Silences"
            className="text-xs"
          >
            Multiple rules cover this target. Cancelling one rule will keep the remaining rules active.
          </Callout>
        )}

        <div className="space-y-2 border rounded p-3 dark:border-gray-700 bg-gray-50 dark:bg-gray-900/40">
          {reasons.map((r) => (
            <label
              key={r.silence_id}
              className={`flex items-start gap-2.5 p-2 rounded cursor-pointer border transition-colors ${
                selectedSilenceId === r.silence_id
                  ? "bg-orange-50 border-orange-300 dark:bg-orange-950/30 dark:border-orange-700"
                  : "bg-white dark:bg-gray-800 border-gray-200 dark:border-gray-700 hover:bg-gray-50"
              }`}
            >
              <input
                type="radio"
                name="silence_rule"
                value={r.silence_id}
                disabled={r.read_only}
                checked={selectedSilenceId === r.silence_id}
                onChange={() => setSelectedSilenceId(r.silence_id)}
                className="mt-0.5 text-orange-600 focus:ring-orange-500"
              />
              <div className="text-xs flex-1">
                <div className="flex items-center gap-1.5 font-mono text-[11px] text-gray-700 dark:text-gray-300">
                  <span className="font-semibold">Rule:</span> {r.silence_id.slice(0, 8)}... (rev {r.revision})
                  <Badge size="xs" color="orange">
                    via {r.via}
                  </Badge>
                </div>
                <div className="text-[11px] text-gray-500 mt-0.5">
                  Ends: {r.ends_at ? new Date(r.ends_at).toLocaleString() : "Indefinite"}
                </div>
                {r.read_only && <div className="text-[11px] text-gray-500">Manage this rule in Alertmanager.</div>}
              </div>
            </label>
          ))}
        </div>

        <div>
          <label className="block text-xs font-semibold text-gray-700 dark:text-gray-300 mb-1">
            Reason for cancellation
          </label>
          <input
            type="text"
            value={cancelReason}
            onChange={(e) => setCancelReason(e.target.value)}
            className="w-full text-xs p-2 border rounded dark:bg-gray-900 dark:border-gray-700"
            placeholder="e.g. Issue resolved early"
          />
        </div>

        {errorMsg && (
          <div className="text-xs text-red-600 bg-red-50 dark:bg-red-900/30 p-2 rounded border border-red-200 dark:border-red-800">
            {errorMsg}
          </div>
        )}

        <div className="flex justify-end gap-2 pt-2 border-t dark:border-gray-700">
          <Button
            type="button"
            variant="secondary"
            color="gray"
            size="xs"
            onClick={onClose}
            disabled={isLoading}
          >
            Close
          </Button>
          <Button
            type="submit"
            color="orange"
            size="xs"
            disabled={isLoading || !hasUpdatePermission || !selectedReason}
            loading={isLoading}
          >
            Cancel Silence Rule
          </Button>
        </div>
      </form>
    </Modal>
  );
}
