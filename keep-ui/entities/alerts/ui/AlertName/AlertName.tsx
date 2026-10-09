import React from "react";
import { AlertDto } from "@/entities/alerts/model";
import { clsx } from "clsx";
import { useAlertRowStyle } from "@/entities/alerts/model/useAlertRowStyle";
import { SilenceBadge } from "@/features/silences/silence-badge";

interface Props {
  alert: AlertDto;
  className?: string;
  expanded?: boolean;
}

export function AlertName({ alert, className, expanded }: Props) {
  const [rowStyle] = useAlertRowStyle();
  const isCompact = rowStyle === "default";

  return (
    <div
      className={clsx(
        "flex items-center gap-1.5",
        expanded ? "max-w-[180px] overflow-hidden" : "",
        className
      )}
    >
      <div
        className={clsx(
          expanded
            ? "whitespace-pre-wrap break-words overflow-hidden max-w-[180px]"
            : isCompact
            ? "truncate whitespace-nowrap"
            : "line-clamp-3 whitespace-pre-wrap",
          expanded ? "" : "flex-grow"
        )}
        title={expanded ? undefined : alert.name}
      >
        {alert.presentation?.title || alert.name}
      </div>
      <SilenceBadge metadata={alert.silence} dismissed={alert.dismissed} />
    </div>
  );
}
