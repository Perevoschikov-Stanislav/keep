import { IncidentDto } from "@/entities/incidents/model";
import { Button } from "@tremor/react";
import { useState, useCallback } from "react";
import { toast } from "react-toastify";
import { KeyedMutator } from "swr";
import { useApi } from "@/shared/lib/hooks/useApi";
import { showErrorToast } from "@/shared/ui";
import { AuditEvent } from "@/entities/alerts/model";
import { useUsers } from "@/entities/users/model/useUsers";
import { extractTaggedUsers } from "../lib/extractTaggedUsers";
import { IncidentCommentInput } from "./IncidentCommentInput.dynamic";
import { useUserPermissions } from "@/shared/lib/hooks/useUserPermissions";

/**
 * Component for adding comments to an incident with user mention capability
 */
export function IncidentActivityComment({
  incident,
  mutator,
}: {
  incident: IncidentDto;
  mutator: KeyedMutator<AuditEvent[]>;
}) {
  const [comment, setComment] = useState("");

  const api = useApi();
  const { can } = useUserPermissions();
  const canComment = can("update:incident", incident);

  const { data: users = [] } = useUsers();
  const onSubmit = useCallback(async () => {
    try {
      const extractedTaggedUsers = extractTaggedUsers(comment);
      await api.post(`/incidents/${incident.id}/comment`, {
        status: incident.status,
        comment,
        tagged_users: extractedTaggedUsers,
      });
      toast.success("Comment added!", { position: "top-right" });
      setComment("");
      mutator();
    } catch (error) {
      showErrorToast(error, "Failed to add comment");
    }
  }, [api, incident.id, incident.status, comment, mutator]);

  return (
    <div className="border border-tremor-border rounded-tremor-default shadow-tremor-input flex flex-col">
      {!canComment && <p className="p-2 text-sm text-gray-500">Read only: commenting requires access to change this incident</p>}
      <fieldset disabled={!canComment}>
      <IncidentCommentInput
        value={comment}
        onValueChange={setComment}
        users={users}
        placeholder="Add a comment..."
        className="min-h-11"
      />

      <div className="flex justify-end p-2">
        <Button
          color="orange"
          variant="primary"
          disabled={!comment || !canComment}
          onClick={onSubmit}
        >
          Comment
        </Button>
      </div>
      </fieldset>
    </div>
  );
}
