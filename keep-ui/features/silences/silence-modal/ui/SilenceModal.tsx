import React, { useState, useEffect, useMemo, useRef } from "react";
import {
  Button,
  Callout,
} from "@tremor/react";
import Modal from "@/components/ui/Modal";
import DatePicker from "react-datepicker";
import "react-datepicker/dist/react-datepicker.css";
import { addHours, addDays, isAfter } from "date-fns";
import { AlertDto } from "@/entities/alerts/model";
import { IncidentDto } from "@/entities/incidents/model";
import {
  Selector,
  SilenceDto,
  useSilenceActions,
} from "@/entities/silences/model";
import { useUserPermissions } from "@/shared/lib/hooks/useUserPermissions";
import { useTeams } from "@/utils/hooks/useTeams";
import { IoNotificationsOffOutline } from "react-icons/io5";

export type DurationOption =
  | "1h"
  | "4h"
  | "8h"
  | "1d"
  | "3d"
  | "7d"
  | "custom"
  | "forever";

interface SilenceModalProps {
  isOpen: boolean;
  onClose: () => void;
  alerts?: AlertDto[] | null;
  incident?: IncidentDto | null;
  silenceToEdit?: SilenceDto | null;
  onSuccess?: (silence: SilenceDto) => void;
}

export function SilenceModal({
  isOpen,
  onClose,
  alerts,
  incident,
  silenceToEdit,
  onSuccess,
}: SilenceModalProps) {
  const { createSilence, updateSilence } = useSilenceActions();
  const { can, permissions } = useUserPermissions();
  const { data: teamsData } = useTeams();

  // Mode resolution
  const isEditMode = !!silenceToEdit;
  const externallyManaged = silenceToEdit?.read_only || silenceToEdit?.origin === "alertmanager";
  const isAlertMode = !isEditMode && !!alerts && alerts.length > 0;
  const isIncidentMode = !isEditMode && !isAlertMode && !!incident;
  const isManualMode = !isEditMode && !isAlertMode && !isIncidentMode;

  // Form states
  const [selectorKind, setSelectorKind] = useState<"alert" | "incident" | "filter">("alert");
  const [manualFingerprints, setManualFingerprints] = useState<string>("");
  const [manualIncidentIds, setManualIncidentIds] = useState<string>("");
  const [manualCel, setManualCel] = useState<string>("");

  const [selectedTeamId, setSelectedTeamId] = useState<string | null>(null);
  const [comment, setComment] = useState<string>("");
  const [durationOption, setDurationOption] = useState<DurationOption>("4h");
  const [customEndDate, setCustomEndDate] = useState<Date | null>(null);
  const [startMode, setStartMode] = useState<"now" | "scheduled">("now");
  const [startDate, setStartDate] = useState<Date | null>(null);
  const pendingSubmission = useRef<{ signature: string; starts_at: string | null; ends_at: string | null } | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [validationError, setValidationError] = useState<string | null>(null);

  const targetKey = JSON.stringify(silenceToEdit ? [silenceToEdit.id, silenceToEdit.revision] :
    alerts?.length ? alerts.map((alert) => [alert.fingerprint, alert.team_id]) : incident?.id ?? null);

  // Refreshing the same targets must not erase a form in progress.
  useEffect(() => {
    if (!isOpen) return;
    pendingSubmission.current = null;
    setStartMode(silenceToEdit?.state === "scheduled" ? "scheduled" : "now");
    setStartDate(silenceToEdit ? new Date(silenceToEdit.starts_at) : null);

    if (silenceToEdit) {
      setComment(silenceToEdit.comment || "");
      setSelectedTeamId(silenceToEdit.team_id || null);
      if (silenceToEdit.selector.kind === "alert") {
        setSelectorKind("alert");
        setManualFingerprints(silenceToEdit.selector.fingerprints.join("\n"));
      } else if (silenceToEdit.selector.kind === "incident") {
        setSelectorKind("incident");
        setManualIncidentIds(silenceToEdit.selector.incident_ids.join("\n"));
      } else if (silenceToEdit.selector.kind === "filter") {
        setSelectorKind("filter");
        setManualCel(silenceToEdit.selector.cel);
      }

      if (silenceToEdit.ends_at) {
        setDurationOption("custom");
        setCustomEndDate(new Date(silenceToEdit.ends_at));
      } else {
        setDurationOption("forever");
        setCustomEndDate(null);
      }
    } else if (isAlertMode && alerts) {
      setSelectorKind("alert");
      const teamId = alerts[0]?.team_id ?? null;
      setSelectedTeamId(teamId);
      setComment("");
      setDurationOption("4h");
      setCustomEndDate(addHours(new Date(), 4));
    } else if (isIncidentMode && incident) {
      setSelectorKind("incident");
      setSelectedTeamId(incident.team_id || null);
      setComment("");
      setDurationOption("4h");
      setCustomEndDate(addHours(new Date(), 4));
    } else {
      setSelectorKind("alert");
      setSelectedTeamId(null);
      setComment("");
      setDurationOption("4h");
      setCustomEndDate(addHours(new Date(), 4));
      setManualFingerprints("");
      setManualIncidentIds("");
      setManualCel("");
    }

    setValidationError(null);
    setIsLoading(false);
  }, [isOpen, targetKey]);

  // Compute calculated end date
  const endFor = (baseDate: Date): Date | null => {
    switch (durationOption) {
      case "1h":
        return addHours(baseDate, 1);
      case "4h":
        return addHours(baseDate, 4);
      case "8h":
        return addHours(baseDate, 8);
      case "1d":
        return addDays(baseDate, 1);
      case "3d":
        return addDays(baseDate, 3);
      case "7d":
        return addDays(baseDate, 7);
      case "forever":
        return null;
      case "custom":
        return customEndDate;
      default:
        return addHours(baseDate, 4);
    }
  };
  const calculatedEndDate = endFor(startMode === "scheduled" && startDate ? startDate : new Date());

  // Team options available
  const availableTeams = useMemo(() => {
    const all = teamsData?.teams ?? [];
    if (permissions?.writable_teams === null || permissions?.writable_teams === undefined) {
      return all;
    }
    return all.filter((t) => permissions.writable_teams?.includes(t.id));
  }, [teamsData, permissions]);
  useEffect(() => {
    if (isOpen && isManualMode && selectedTeamId === null && availableTeams.length &&
        !can("write:silence", { team_id: null })) {
      setSelectedTeamId(availableTeams[0].id);
    }
  }, [isOpen, isManualMode, availableTeams, selectedTeamId]);

  // Permission check
  const requiredScope = isEditMode ? "update:silence" : "write:silence";
  const mixedTeams = !!alerts?.length && new Set(alerts.map((alert) => alert.team_id ?? null)).size > 1;
  const hasPermission = !mixedTeams && can(requiredScope, { team_id: selectedTeamId });

  // Resolve selector
  const resolveSelector = (): Selector | null => {
    if (isAlertMode && alerts) {
      const fingerprints = Array.from(new Set(alerts.map((a) => a.fingerprint.trim()).filter(Boolean)));
      if (fingerprints.length === 0) return null;
      return { kind: "alert", fingerprints };
    }
    if (isIncidentMode && incident) {
      return { kind: "incident", incident_ids: [incident.id] };
    }

    // Manual mode
    if (selectorKind === "alert") {
      const fps = Array.from(
        new Set(
          manualFingerprints
            .split(/[\n,]/)
            .map((s) => s.trim())
            .filter(Boolean)
        )
      );
      if (fps.length === 0) return null;
      return { kind: "alert", fingerprints: fps };
    }
    if (selectorKind === "incident") {
      const ids = Array.from(
        new Set(
          manualIncidentIds
            .split(/[\n,]/)
            .map((s) => s.trim())
            .filter(Boolean)
        )
      );
      if (ids.length === 0) return null;
      return { kind: "incident", incident_ids: ids };
    }
    if (selectorKind === "filter") {
      const cel = manualCel.trim();
      if (!cel) return null;
      return { kind: "filter", cel };
    }
    return null;
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setValidationError(null);
    if (!hasPermission || isLoading || externallyManaged) return;

    if (!comment.trim()) {
      setValidationError("Comment / reason is required");
      return;
    }

    const selector = resolveSelector();
    if (!selector) {
      setValidationError("Valid selector targets are required (at least one fingerprint, incident ID, or CEL filter)");
      return;
    }

    const signature = JSON.stringify([selector, selectedTeamId, comment.trim(), durationOption,
      customEndDate?.toISOString(), startMode, startDate?.toISOString(), silenceToEdit?.revision]);
    let submission = pendingSubmission.current?.signature === signature ? pendingSubmission.current : null;
    if (!submission) {
      const now = new Date();
      const start = startMode === "scheduled" ? startDate : now;
      if (!start || (startMode === "scheduled" && !isAfter(start, now))) {
        setValidationError("Scheduled start must be in the future");
        return;
      }
      if (durationOption === "custom" && !customEndDate) {
        setValidationError("Please select an end date and time");
        return;
      }
      const end = endFor(start);
      if (end && (!isAfter(end, now) || !isAfter(end, start))) {
        setValidationError("End date must be after the start and in the future");
        return;
      }
      submission = { signature, starts_at: startMode === "now" ? null : start.toISOString(),
        ends_at: end?.toISOString() ?? null };
      pendingSubmission.current = submission;
    }

    setIsLoading(true);
    try {
      if (isEditMode && silenceToEdit) {
        const result = await updateSilence(silenceToEdit.id, silenceToEdit.revision, {
          selector,
          ...(silenceToEdit.state === "scheduled" && submission.starts_at ? { starts_at: submission.starts_at } : {}),
          ends_at: submission.ends_at,
          comment: comment.trim(),
        });
        if (result) {
          onSuccess?.(result);
          onClose();
        }
      } else {
        const result = await createSilence({
          team_id: selectedTeamId,
          selector,
          starts_at: submission.starts_at,
          ends_at: submission.ends_at,
          comment: comment.trim(),
        });
        if (result) {
          onSuccess?.(result);
          onClose();
        }
      }
    } catch (err: any) {
      const detail = err?.responseJson?.detail ?? err?.response?.data?.detail;
      setValidationError(detail?.code === "revision_conflict"
        ? "This rule changed. Close this form and reopen it to review the current version."
        : detail?.message || err?.message || "Failed to save silence");
      if (err?.statusCode >= 400 && err?.statusCode < 500) pendingSubmission.current = null;
    } finally {
      setIsLoading(false);
    }
  };

  const titleText = isEditMode
    ? "Edit Silence Rule"
    : isAlertMode
    ? alerts && alerts.length > 1
      ? `Silence ${alerts.length} Alerts`
      : `Silence Alert: ${alerts?.[0]?.name || alerts?.[0]?.fingerprint}`
    : isIncidentMode
    ? `Silence Incident #${incident?.id}`
    : "Create Silence Rule";

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      className="w-full max-w-xl overflow-visible p-6 bg-white dark:bg-gray-800 rounded-lg shadow-xl"
      title={titleText}
    >
      <form onSubmit={handleSubmit} className="space-y-4">
        {/* Context description callout */}
        <Callout
          icon={IoNotificationsOffOutline}
          color="orange"
          title={
            isAlertMode
              ? "Silence Alert Notifications"
              : isIncidentMode
              ? "Silence Incident Notifications"
            : "Silence Notifications"
          }
          className="text-xs"
        >
          {isAlertMode
            ? "Incoming events for this alert fingerprint will be suppressed from triggering notifications until the rule expires."
            : isIncidentMode
            ? "Notifications for alerts correlated with this incident will be suppressed until the rule expires."
            : "Suppresses outgoing notifications without losing incoming telemetry."}
        </Callout>

        {/* Target display / selector */}
        {isAlertMode && alerts && (
          <div className="bg-slate-50 dark:bg-slate-900/50 p-3 rounded border border-slate-200 dark:border-slate-700 text-xs space-y-1">
            <span className="font-semibold text-slate-700 dark:text-slate-300">Target Fingerprints:</span>
            <div className="max-h-24 overflow-y-auto font-mono text-[11px] text-slate-600 dark:text-slate-400 break-all space-y-0.5">
              {alerts.map((a) => (
                <div key={a.fingerprint} className="truncate">
                  {a.name ? `${a.name}: ` : ""}{a.fingerprint}
                </div>
              ))}
            </div>
          </div>
        )}

        {isIncidentMode && incident && (
          <div className="bg-slate-50 dark:bg-slate-900/50 p-3 rounded border border-slate-200 dark:border-slate-700 text-xs">
            <span className="font-semibold text-slate-700 dark:text-slate-300">Incident: </span>
            <span className="text-slate-600 dark:text-slate-400 font-mono">{incident.id}</span>
            {incident.user_summary && (
              <p className="mt-1 text-slate-600 dark:text-slate-400 truncate">{incident.user_summary}</p>
            )}
          </div>
        )}

        {(isManualMode || isEditMode) && (
          <div className="space-y-3">
            <div>
              <label className="block text-xs font-semibold text-gray-700 dark:text-gray-300 mb-1">
                Target Type
              </label>
              <div className="flex gap-2">
                <Button
                  type="button"
                  size="xs"
                  variant={selectorKind === "alert" ? "primary" : "secondary"}
                  color="orange"
                  onClick={() => setSelectorKind("alert")}
                >
                  Alert Fingerprints
                </Button>
                <Button
                  type="button"
                  size="xs"
                  variant={selectorKind === "incident" ? "primary" : "secondary"}
                  color="orange"
                  onClick={() => setSelectorKind("incident")}
                >
                  Incident IDs
                </Button>
                <Button
                  type="button"
                  size="xs"
                  variant={selectorKind === "filter" ? "primary" : "secondary"}
                  color="orange"
                  onClick={() => setSelectorKind("filter")}
                >
                  CEL Filter
                </Button>
              </div>
            </div>

            {selectorKind === "alert" && (
              <div>
                <label htmlFor="silence-fingerprints" className="block text-xs text-gray-600 dark:text-gray-400 mb-1">
                  Fingerprints (one per line or comma-separated)
                </label>
                <textarea
                  id="silence-fingerprints"
                  rows={3}
                  value={manualFingerprints}
                  onChange={(e) => setManualFingerprints(e.target.value)}
                  placeholder="e.g. 5d41402abc4b2a76b9719d911017c592"
                  className="w-full text-xs font-mono p-2 border rounded dark:bg-gray-900 dark:border-gray-700"
                />
              </div>
            )}

            {selectorKind === "incident" && (
              <div>
                <label htmlFor="silence-incidents" className="block text-xs text-gray-600 dark:text-gray-400 mb-1">
                  Incident IDs (UUID, one per line or comma-separated)
                </label>
                <textarea
                  id="silence-incidents"
                  rows={2}
                  value={manualIncidentIds}
                  onChange={(e) => setManualIncidentIds(e.target.value)}
                  placeholder="e.g. 11111111-1111-4111-8111-111111111111"
                  className="w-full text-xs font-mono p-2 border rounded dark:bg-gray-900 dark:border-gray-700"
                />
              </div>
            )}

            {selectorKind === "filter" && (
              <div>
                <label htmlFor="silence-filter" className="block text-xs text-gray-600 dark:text-gray-400 mb-1">
                  Common Expression Language (CEL) expression
                </label>
                <textarea
                  id="silence-filter"
                  rows={2}
                  value={manualCel}
                  onChange={(e) => setManualCel(e.target.value)}
                  placeholder='e.g. service == "payment" && severity == "critical"'
                  className="w-full text-xs font-mono p-2 border rounded dark:bg-gray-900 dark:border-gray-700"
                />
              </div>
            )}
          </div>
        )}

        {(!isEditMode || silenceToEdit?.state === "scheduled") && (
          <div>
            <label id="silence-start-label" htmlFor="silence-start-mode" className="block text-xs font-semibold mb-1">Start</label>
            <select id="silence-start-mode" value={startMode}
              onChange={(event) => setStartMode(event.target.value as "now" | "scheduled")}
              disabled={isEditMode}
              className="w-full text-xs p-2 border rounded dark:bg-gray-900 dark:border-gray-700">
              <option value="now">Now</option>
              <option value="scheduled">Schedule</option>
            </select>
            {startMode === "scheduled" && (
              <DatePicker selected={startDate} onChange={(date: Date | null) => setStartDate(date)}
                ariaLabelledBy="silence-start-label" showTimeSelect timeIntervals={15}
                dateFormat="yyyy-MM-dd HH:mm" minDate={new Date()}
                placeholderText="Start date and time" />
            )}
          </div>
        )}

        {/* Duration selector */}
        <div>
          <label className="block text-xs font-semibold text-gray-700 dark:text-gray-300 mb-1">
            Duration
          </label>
          <div className="grid grid-cols-4 gap-1.5 mb-2">
            {(["1h", "4h", "8h", "1d", "3d", "7d", "forever", "custom"] as DurationOption[]).map(
              (opt) => (
                <button
                  key={opt}
                  type="button"
                  onClick={() => setDurationOption(opt)}
                  className={`py-1.5 px-2 text-xs font-medium rounded border transition-colors ${
                    durationOption === opt
                      ? "bg-orange-500 text-white border-orange-600 dark:bg-orange-600"
                      : "bg-gray-50 text-gray-700 border-gray-200 hover:bg-gray-100 dark:bg-gray-800 dark:text-gray-300 dark:border-gray-700 dark:hover:bg-gray-700"
                  }`}
                >
                  {opt === "1h" && "1 Hour"}
                  {opt === "4h" && "4 Hours"}
                  {opt === "8h" && "8 Hours"}
                  {opt === "1d" && "1 Day"}
                  {opt === "3d" && "3 Days"}
                  {opt === "7d" && "1 Week"}
                  {opt === "forever" && "Indefinite"}
                  {opt === "custom" && "Custom..."}
                </button>
              )
            )}
          </div>

          {durationOption === "custom" && (
            <div className="mt-2 p-2.5 bg-slate-50 dark:bg-slate-900/50 rounded border border-slate-200 dark:border-slate-700 flex flex-col items-center">
              <label htmlFor="silence-end" className="text-xs text-slate-600 dark:text-slate-400 mb-1 font-medium">
                Select End Date & Time (Local):
              </label>
              <DatePicker
                id="silence-end"
                selected={customEndDate}
                onChange={(date: Date | null) => setCustomEndDate(date)}
                showTimeSelect
                timeIntervals={15}
                dateFormat="yyyy-MM-dd HH:mm"
                minDate={new Date()}
                className="text-xs text-center p-1.5 border rounded dark:bg-gray-900 dark:border-gray-700"
              />
            </div>
          )}

          {calculatedEndDate ? (
            <div className="text-[11px] text-gray-500 mt-1">
              Expires at: <span className="font-mono text-gray-700 dark:text-gray-300">{calculatedEndDate.toLocaleString()}</span> (UTC: {calculatedEndDate.toISOString()})
            </div>
          ) : (
            <div className="text-[11px] text-orange-600 dark:text-orange-400 mt-1 font-medium">
              Silence is indefinite and will remain active until manually cancelled.
            </div>
          )}
        </div>

        {/* Team selector */}
        {availableTeams.length > 0 && isManualMode && (
          <div>
            <label htmlFor="silence-team" className="block text-xs font-semibold text-gray-700 dark:text-gray-300 mb-1">
              Team Scope
            </label>
            <select
              id="silence-team"
              value={selectedTeamId || ""}
              onChange={(e) => setSelectedTeamId(e.target.value || null)}
              className="w-full text-xs p-2 border rounded dark:bg-gray-900 dark:border-gray-700"
            >
              {can(requiredScope, { team_id: null }) && <option value="">Unassigned objects only</option>}
              {availableTeams.map((team) => (
                <option key={team.id} value={team.id}>
                  Team: {team.id}
                </option>
              ))}
            </select>
          </div>
        )}
        {!isManualMode && <div className="text-xs">Team: {selectedTeamId ?? "Unassigned objects only"}</div>}
        {mixedTeams && <div role="alert" className="text-xs text-red-600">Select alerts from one team to create a silence.</div>}

        {/* Comment field */}
        <div>
          <label htmlFor="silence-comment" className="block text-xs font-semibold text-gray-700 dark:text-gray-300 mb-1">
            Reason / Comment <span className="text-red-500">*</span>
          </label>
          <textarea
            id="silence-comment"
            rows={2}
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            placeholder="e.g. Scheduled maintenance on payment gateway or Investigating spike"
            className="w-full text-xs p-2 border rounded dark:bg-gray-900 dark:border-gray-700"
          />
        </div>

        {/* Validation error */}
        {validationError && (
          <div className="text-xs text-red-600 bg-red-50 dark:bg-red-900/30 p-2 rounded border border-red-200 dark:border-red-800">
            {validationError}
          </div>
        )}

        {/* Permission warning */}
        {externallyManaged && <div role="alert" className="text-xs text-amber-700">Manage this rule in Alertmanager.</div>}
        {!hasPermission && (
          <div className="text-xs text-amber-700 bg-amber-50 dark:bg-amber-900/30 p-2 rounded border border-amber-200 dark:border-amber-800">
            You do not have permission to manage silences for this team ({requiredScope}).
          </div>
        )}

        {/* Actions */}
        <div className="flex justify-end gap-2 pt-2 border-t dark:border-gray-700">
          <Button
            type="button"
            variant="secondary"
            color="gray"
            size="xs"
            onClick={onClose}
            disabled={isLoading}
          >
            Cancel
          </Button>
          <Button
            type="submit"
            color="orange"
            size="xs"
            disabled={isLoading || !hasPermission || externallyManaged}
            loading={isLoading}
          >
            {isEditMode ? "Save Changes" : "Apply Silence"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
