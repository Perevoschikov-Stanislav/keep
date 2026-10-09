import React from "react";
import { Badge } from "@tremor/react";
import { SilenceMetadata, SilenceReason } from "@/entities/silences/model";
import { IoNotificationsOffOutline } from "react-icons/io5";

interface SilenceBadgeProps {
  metadata?: SilenceMetadata | null;
  dismissed?: boolean;
  size?: "xs" | "sm";
  className?: string;
  isIncident?: boolean;
}

export function SilenceBadge({
  metadata,
  dismissed,
  size = "xs",
  className = "",
  isIncident = false,
}: SilenceBadgeProps) {
  const isSilenced = !!metadata?.silenced;
  const isPartial = isIncident && metadata?.coverage === "partial";
  const isLegacyDismissed = !isSilenced && !isPartial && !!dismissed;

  if (!isSilenced && !isPartial && !isLegacyDismissed) {
    return null;
  }

  const formatTooltip = (meta: SilenceMetadata | undefined | null) => {
    if (!meta) return "Dismissed";
    const untilStr = meta.silenced_until
      ? `until ${new Date(meta.silenced_until).toLocaleString()}`
      : "indefinitely (forever)";

    const reasonsList = (meta.reasons || [])
      .map((r: SilenceReason) => `[${r.via}] ${r.silence_id.slice(0, 8)}...`)
      .join(", ");

    if (isPartial) {
      return `Partially silenced: ${meta.silenced_alerts} of ${meta.total_alerts} alerts${
        reasonsList ? ` (${reasonsList})` : ""
      }`;
    }

    return `Silenced ${untilStr}${reasonsList ? ` (via: ${reasonsList})` : ""}`;
  };

  if (isPartial && metadata) {
    return (
      <Badge
        size={size}
        color="amber"
        icon={IoNotificationsOffOutline}
        tooltip={formatTooltip(metadata)}
        className={className}
      >
        Silenced {metadata.silenced_alerts}/{metadata.total_alerts}
      </Badge>
    );
  }

  if (isSilenced) {
    return (
      <Badge
        size={size}
        color="slate"
        icon={IoNotificationsOffOutline}
        tooltip={formatTooltip(metadata)}
        className={className}
      >
        Silenced
      </Badge>
    );
  }

  if (isLegacyDismissed) {
    return (
      <Badge
        size={size}
        color="gray"
        icon={IoNotificationsOffOutline}
        tooltip="Dismissed (legacy)"
        className={className}
      >
        Dismissed
      </Badge>
    );
  }

  return null;
}
