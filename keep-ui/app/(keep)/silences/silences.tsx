"use client";

import React, { useState, useMemo } from "react";
import {
  Badge,
  Button,
  Card,
  Subtitle,
  Tab,
  TabGroup,
  TabList,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeaderCell,
  TableRow,
} from "@tremor/react";
import {
  SilenceDto,
  SilenceState,
  useSilences,
  useSilenceActions,
} from "@/entities/silences/model";
import { SilenceModal } from "@/features/silences/silence-modal";
import { useUserPermissions } from "@/shared/lib/hooks/useUserPermissions";
import { useTeams } from "@/utils/hooks/useTeams";
import { EmptyStateCard } from "@/shared/ui";
import { IoNotificationsOffOutline, IoAdd, IoClose } from "react-icons/io5";
import { MdModeEdit } from "react-icons/md";
import { addHours } from "date-fns";

export default function Silences() {
  const [selectedState, setSelectedState] = useState<SilenceState | "all">("active");
  const [selectedTeamId, setSelectedTeamId] = useState<string | null | undefined>(undefined);
  const [cursor, setCursor] = useState<string | null>(null);
  const [previousCursors, setPreviousCursors] = useState<Array<string | null>>([]);
  const [searchQuery, setSearchQuery] = useState<string>("");

  const [isCreateModalOpen, setIsCreateModalOpen] = useState<boolean>(false);
  const [editingSilence, setEditingSilence] = useState<SilenceDto | null>(null);
  const [cancellingSilence, setCancellingSilence] = useState<SilenceDto | null>(null);
  const [cancelReason, setCancelReason] = useState<string>("Cancelled via UI");
  const [isCancelling, setIsCancelling] = useState<boolean>(false);

  const { can } = useUserPermissions();
  const { data: teamsData } = useTeams();

  const {
    silences,
    isLoading,
    error,
    mutate,
    nextCursor,
  } = useSilences({
    state: selectedState,
    team_id: selectedTeamId,
    cursor,
  });

  const { updateSilence, cancelSilence } = useSilenceActions();

  // Search filter
  const filteredSilences = useMemo(() => {
    if (!searchQuery.trim()) return silences;
    const q = searchQuery.toLowerCase().trim();
    return silences.filter((s) => {
      if (s.id.toLowerCase().includes(q)) return true;
      if (s.comment.toLowerCase().includes(q)) return true;
      if (s.created_by.display_name.toLowerCase().includes(q)) return true;
      if (s.team_id && s.team_id.toLowerCase().includes(q)) return true;
      if (s.selector.kind === "filter" && s.selector.cel.toLowerCase().includes(q)) return true;
      if (s.selector.kind === "alert" && s.selector.fingerprints.some((fp) => fp.toLowerCase().includes(q))) return true;
      if (s.selector.kind === "incident" && s.selector.incident_ids.some((id) => id.toLowerCase().includes(q))) return true;
      return false;
    });
  }, [silences, searchQuery]);

  const handleQuickExtend = async (silence: SilenceDto, hours: number) => {
    const baseDate = silence.ends_at ? new Date(silence.ends_at) : new Date();
    const effectiveBase = baseDate > new Date() ? baseDate : new Date();
    const newEndDate = addHours(effectiveBase, hours);

    try {
      await updateSilence(silence.id, silence.revision, {
        ends_at: newEndDate.toISOString(),
      });
      await mutate();
    } catch {
      // toast shown in hook
    }
  };

  const handleConfirmCancel = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!cancellingSilence) return;

    setIsCancelling(true);
    try {
      await cancelSilence(
        cancellingSilence.id,
        cancellingSilence.revision,
        cancelReason.trim() || "Cancelled via UI"
      );
      setCancellingSilence(null);
      await mutate();
    } catch {
      // toast shown in hook
    } finally {
      setIsCancelling(false);
    }
  };

  const getStateBadgeColor = (state: SilenceState) => {
    switch (state) {
      case "active":
        return "emerald";
      case "scheduled":
        return "sky";
      case "expired":
        return "slate";
      case "cancelled":
        return "rose";
      default:
        return "gray";
    }
  };

  const stateTabs: Array<{ label: string; value: SilenceState | "all" }> = [
    { label: "Active", value: "active" },
    { label: "Scheduled", value: "scheduled" },
    { label: "All Rules", value: "all" },
    { label: "Expired", value: "expired" },
    { label: "Cancelled", value: "cancelled" },
  ];

  const canCreate = can("write:silence");

  return (
    <div className="space-y-4">
      {/* Header and Controls */}
      <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-3">
        <div>
          <h1 className="text-xl font-bold text-gray-900 dark:text-gray-100 flex items-center gap-2">
            <IoNotificationsOffOutline className="text-orange-500" /> Silences Registry
          </h1>
          <Subtitle className="text-xs text-gray-500">
            Manage notification suppression rules across alert fingerprints, incidents, and CEL filters.
          </Subtitle>
        </div>

        <Button
          icon={IoAdd}
          color="orange"
          size="xs"
          disabled={!canCreate}
          title={!canCreate ? "You do not have write:silence permission" : undefined}
          onClick={() => setIsCreateModalOpen(true)}
        >
          New Silence Rule
        </Button>
      </div>

      {/* Filters bar */}
      <Card className="p-3">
        <div className="flex flex-col md:flex-row justify-between items-stretch md:items-center gap-3">
          <TabGroup
            index={stateTabs.findIndex((t) => t.value === selectedState)}
            onIndexChange={(idx) => {
              setSelectedState(stateTabs[idx].value);
              setCursor(null);
              setPreviousCursors([]);
            }}
          >
            <TabList variant="solid" color="orange">
              {stateTabs.map((t) => (
                <Tab key={t.value} className="text-xs py-1 px-3">
                  {t.label}
                </Tab>
              ))}
            </TabList>
          </TabGroup>

          <div className="flex flex-wrap items-center gap-2">
            {/* Team filter */}
            {teamsData?.teams && teamsData.teams.length > 0 && (
              <select
                aria-label="Team filter"
                value={selectedTeamId === undefined ? "all" : JSON.stringify(selectedTeamId)}
                onChange={(e) => {
                  setSelectedTeamId(e.target.value === "all" ? undefined : JSON.parse(e.target.value));
                  setCursor(null);
                  setPreviousCursors([]);
                }}
                className="text-xs p-1.5 border rounded dark:bg-gray-800 dark:border-gray-700 text-gray-700 dark:text-gray-300"
              >
                <option value="all">All Teams</option>
                <option value="null">Unassigned objects only</option>
                {teamsData.teams.map((t) => (
                  <option key={t.id} value={JSON.stringify(t.id)}>
                    Team: {t.id}
                  </option>
                ))}
              </select>
            )}

            {/* Search input */}
            <input
              type="text"
              placeholder="Search this page by comment, target, actor..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="text-xs p-1.5 border rounded w-48 sm:w-64 dark:bg-gray-800 dark:border-gray-700 text-gray-700 dark:text-gray-300"
            />
          </div>
        </div>
      </Card>

      {/* Table / Content */}
      <Card className="p-0 overflow-hidden">
        {isLoading ? (
          <div className="p-8 text-center text-xs text-gray-500">
            Loading silence rules...
          </div>
        ) : error ? (
          <div className="p-6 text-center text-xs text-red-600">
            Failed to load silences: {error.message || "Unknown error"}
          </div>
        ) : filteredSilences.length === 0 ? (
          <div className="p-8">
            <EmptyStateCard
              noCard
              icon={IoNotificationsOffOutline}
              title="No silences found"
              description={
                selectedState === "active"
                  ? "There are no active notification silence rules right now."
                  : "No silence rules match the selected filter criteria."
              }
            />
          </div>
        ) : (
          <div className="overflow-x-auto">
            <Table className="text-xs">
              <TableHead className="bg-gray-50 dark:bg-gray-900/50">
                <TableRow>
                  <TableHeaderCell className="w-24">State</TableHeaderCell>
                  <TableHeaderCell>Scope / Targets</TableHeaderCell>
                  <TableHeaderCell>Reason / Comment</TableHeaderCell>
                  <TableHeaderCell className="w-28">Team</TableHeaderCell>
                  <TableHeaderCell className="w-48">Period (UTC)</TableHeaderCell>
                  <TableHeaderCell className="w-36">Author / Origin</TableHeaderCell>
                  <TableHeaderCell className="w-32 text-right">Actions</TableHeaderCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {filteredSilences.map((silence) => {
                  const externallyManaged = silence.read_only || silence.origin === "alertmanager";
                  const canEdit = !externallyManaged && can("update:silence", silence);
                  const readOnlyMessage = externallyManaged ? "Manage this rule in Alertmanager" : "Read only: no permission for this team";
                  const isIndefinite = !silence.ends_at;

                  return (
                    <TableRow
                      key={silence.id}
                      className="hover:bg-gray-50/80 dark:hover:bg-gray-800/50 transition-colors"
                    >
                      {/* State */}
                      <TableCell>
                        <Badge size="xs" color={getStateBadgeColor(silence.state)}>
                          {silence.state}
                        </Badge>
                      </TableCell>

                      {/* Scope / Targets */}
                      <TableCell>
                        {silence.selector.kind === "alert" && (
                          <div className="space-y-0.5">
                            <div className="flex items-center gap-1 font-semibold text-gray-800 dark:text-gray-200">
                              <span>Alerts</span>
                              <Badge size="xs" color="gray">
                                {silence.selector.fingerprints.length}
                              </Badge>
                            </div>
                            <div className="font-mono text-[10px] text-gray-500 truncate max-w-xs">
                              {silence.selector.fingerprints.slice(0, 2).join(", ")}
                              {silence.selector.fingerprints.length > 2 && "..."}
                            </div>
                          </div>
                        )}
                        {silence.selector.kind === "incident" && (
                          <div className="space-y-0.5">
                            <div className="flex items-center gap-1 font-semibold text-gray-800 dark:text-gray-200">
                              <span>Incidents</span>
                              <Badge size="xs" color="gray">
                                {silence.selector.incident_ids.length}
                              </Badge>
                            </div>
                            <div className="font-mono text-[10px] text-gray-500 truncate max-w-xs">
                              {silence.selector.incident_ids.slice(0, 2).join(", ")}
                              {silence.selector.incident_ids.length > 2 && "..."}
                            </div>
                          </div>
                        )}
                        {silence.selector.kind === "filter" && (
                          <div className="space-y-0.5">
                            <span className="font-semibold text-gray-800 dark:text-gray-200">CEL Filter</span>
                            <div className="font-mono text-[10px] text-gray-600 dark:text-gray-400 bg-gray-100 dark:bg-gray-800 px-1 py-0.5 rounded truncate max-w-xs">
                              {silence.selector.cel}
                            </div>
                          </div>
                        )}
                      </TableCell>

                      {/* Comment */}
                      <TableCell>
                        <div className="font-medium text-gray-800 dark:text-gray-200 max-w-xs truncate" title={silence.comment}>
                          {silence.comment || "—"}
                        </div>
                        <div className="text-[10px] text-gray-400 font-mono">
                          ID: {silence.id.slice(0, 8)}... (rev {silence.revision})
                        </div>
                      </TableCell>

                      {/* Team */}
                      <TableCell>
                        {silence.team_id ? (
                          <Badge size="xs" color="orange">
                            {silence.team_id}
                          </Badge>
                        ) : (
                          <span className="text-gray-400 italic">Unassigned</span>
                        )}
                      </TableCell>

                      {/* Period */}
                      <TableCell>
                        <div className="text-[11px] text-gray-700 dark:text-gray-300">
                          {new Date(silence.starts_at).toISOString()}
                        </div>
                        <div className="text-[11px] text-gray-500">
                          to {isIndefinite ? <span className="text-orange-600 font-medium">Forever</span> : new Date(silence.ends_at!).toISOString()}
                        </div>
                      </TableCell>

                      {/* Author / Origin */}
                      <TableCell>
                        <div className="text-gray-700 dark:text-gray-300 font-medium truncate max-w-[120px]" title={silence.created_by.display_name}>
                          {silence.created_by.display_name}
                        </div>
                        <Badge size="xs" color="gray" className="text-[10px] mt-0.5">
                          {silence.origin || "ui"}
                        </Badge>
                        {externallyManaged && <div className="text-[11px] text-gray-500 mt-1">Manage in Alertmanager</div>}
                        {silence.synchronization?.map((sync) => (
                          <div key={sync.source_id} className="text-[11px] text-gray-500 mt-1">
                            {sync.reason === "legacy_binding_requires_review" ? "Legacy Alertmanager link needs review"
                              : sync.reason === "team_scope_unconfigured" ? "Keep only: Alertmanager team boundary is not configured"
                              : sync.state === "local_only" ? "Keep only: no equivalent Alertmanager rule"
                              : sync.state === "uncertain" ? "Alertmanager sync needs attention"
                              : sync.state === "synced" ? "Synced to Alertmanager" : null}
                          </div>
                        ))}
                      </TableCell>

                      {/* Actions */}
                      <TableCell className="text-right">
                        <div className="flex items-center justify-end gap-1">
                          {silence.state === "active" && (
                            <>
                              <Button
                                size="xs"
                                variant="secondary"
                                color="orange"
                                disabled={!canEdit || isIndefinite}
                                title={!canEdit ? readOnlyMessage : isIndefinite ? "Already indefinite" : "Extend by 4 hours"}
                                onClick={() => handleQuickExtend(silence, 4)}
                              >
                                +4h
                              </Button>
                              <Button
                                size="xs"
                                variant="secondary"
                                color="orange"
                                icon={MdModeEdit}
                                disabled={!canEdit}
                                title={!canEdit ? readOnlyMessage : "Edit rule"}
                                onClick={() => setEditingSilence(silence)}
                              />
                              <Button
                                size="xs"
                                variant="secondary"
                                color="red"
                                icon={IoClose}
                                disabled={!canEdit}
                                title={!canEdit ? readOnlyMessage : "Cancel silence"}
                                onClick={() => {
                                  setCancellingSilence(silence);
                                  setCancelReason("Cancelled via UI");
                                }}
                              />
                            </>
                          )}
                          {silence.state === "scheduled" && (
                            <>
                              <Button
                                size="xs"
                                variant="secondary"
                                color="orange"
                                icon={MdModeEdit}
                                disabled={!canEdit}
                                onClick={() => setEditingSilence(silence)}
                              />
                              <Button
                                size="xs"
                                variant="secondary"
                                color="red"
                                icon={IoClose}
                                disabled={!canEdit}
                                onClick={() => {
                                  setCancellingSilence(silence);
                                  setCancelReason("Cancelled via UI");
                                }}
                              />
                            </>
                          )}
                          {(silence.state === "expired" || silence.state === "cancelled") && (
                            <span className="text-[11px] text-gray-400 italic">No actions</span>
                          )}
                        </div>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </div>
        )}
      </Card>

      <div className="flex items-center justify-end gap-2 text-xs">
        <span>Page {previousCursors.length + 1}</span>
        <Button size="xs" variant="secondary" disabled={!previousCursors.length || isLoading}
          onClick={() => {
            setCursor(previousCursors[previousCursors.length - 1]);
            setPreviousCursors(previousCursors.slice(0, -1));
          }}>Previous</Button>
        <Button size="xs" variant="secondary" disabled={!nextCursor || isLoading}
          onClick={() => {
            setPreviousCursors([...previousCursors, cursor]);
            setCursor(nextCursor);
          }}>Next</Button>
      </div>

      {/* Create / Edit Modal */}
      <SilenceModal
        isOpen={isCreateModalOpen || !!editingSilence}
        onClose={() => {
          setIsCreateModalOpen(false);
          setEditingSilence(null);
        }}
        silenceToEdit={editingSilence}
        onSuccess={() => mutate()}
      />

      {/* Cancel Confirmation Modal */}
      {cancellingSilence && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
          <Card className="w-full max-w-md p-6 bg-white dark:bg-gray-800 space-y-4">
            <h3 className="text-base font-bold text-gray-900 dark:text-gray-100">
              Cancel Silence Rule
            </h3>
            <Subtitle className="text-xs text-gray-600 dark:text-gray-400">
              Are you sure you want to cancel silence rule{" "}
              <span className="font-mono font-semibold">{cancellingSilence.id.slice(0, 8)}...</span>?
              Outgoing notifications will be restored.
            </Subtitle>

            <div>
              <label className="block text-xs font-semibold text-gray-700 dark:text-gray-300 mb-1">
                Cancellation Reason
              </label>
              <input
                type="text"
                value={cancelReason}
                onChange={(e) => setCancelReason(e.target.value)}
                placeholder="e.g. Issue resolved early"
                className="w-full text-xs p-2 border rounded dark:bg-gray-900 dark:border-gray-700"
              />
            </div>

            <div className="flex justify-end gap-2 pt-2 border-t dark:border-gray-700">
              <Button
                type="button"
                variant="secondary"
                color="gray"
                size="xs"
                onClick={() => setCancellingSilence(null)}
                disabled={isCancelling}
              >
                Keep Active
              </Button>
              <Button
                type="button"
                color="red"
                size="xs"
                onClick={handleConfirmCancel}
                disabled={isCancelling}
                loading={isCancelling}
              >
                Confirm Cancel
              </Button>
            </div>
          </Card>
        </div>
      )}
    </div>
  );
}
