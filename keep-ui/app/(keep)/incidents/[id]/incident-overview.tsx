"use client";

import {
  useIncidentActions,
  type IncidentDto,
} from "@/entities/incidents/model";
import React, { useState } from "react";
import { useIncident, useIncidentAlerts } from "@/utils/hooks/useIncidents";
import { Disclosure } from "@headlessui/react";
import { IoChevronDown } from "react-icons/io5";
import { Badge, Callout } from "@tremor/react";
import { Button, DynamicImageProviderIcon, Link } from "@/components/ui";
import { IncidentChangeStatusSelect } from "features/incidents/change-incident-status";
import { getIncidentName } from "@/entities/incidents/lib/utils";
import { DateTimeField, FieldHeader } from "@/shared/ui";
import {
  SameIncidentField,
  FollowingIncidents,
} from "@/features/incidents/same-incidents-in-the-past/";
import { StatusIcon } from "@/entities/incidents/ui/statuses";
import clsx from "clsx";
import { TbSparkles } from "react-icons/tb";
import {
  CopilotTask,
  useCopilotAction,
  useCopilotContext,
  useCopilotReadable,
} from "@copilotkit/react-core";
import { IncidentOverviewSkeleton } from "../incident-overview-skeleton";
import { AlertDto } from "@/entities/alerts/model";
import { useRouter } from "next/navigation";
import { RootCauseAnalysis } from "@/components/ui/RootCauseAnalysis";
import { IncidentChangeSeveritySelect } from "features/incidents/change-incident-severity";
import { useApi } from "@/shared/lib/hooks/useApi";
import { startCase, map } from "lodash";
import { useConfig } from "@/utils/hooks/useConfig";
import { EnrichmentEditableField } from "@/app/(keep)/incidents/[id]/enrichments/EnrichmentEditableField";
import { EnrichmentEditableForm } from "@/app/(keep)/incidents/[id]/enrichments/EnrichmentEditableForm";
import { FormattedContent } from "@/shared/ui/FormattedContent/FormattedContent";
import { FiExternalLink } from "react-icons/fi";

const PROVISIONED_ENRICHMENTS = [
  "services",
  "incident_id",
  "incident_url",
  "incident_provider",
  "incident_title",
  "environments",
  "repositories",
  "rca_points",
  "traces",
];

export function isEnrichmentHidden(
  key: string,
  customHidden?: string[]
): boolean {
  const lowerKey = key.toLowerCase();
  if (PROVISIONED_ENRICHMENTS.indexOf(key) > -1) return true;

  // Default integration internal fields that shouldn't clutter the card
  const defaultHiddenPrefixes = ["mm_", "mm ", "snooze_", "snooze ", "_"];
  if (defaultHiddenPrefixes.some((p) => lowerKey.startsWith(p))) return true;

  if (customHidden && customHidden.length > 0) {
    for (const pattern of customHidden) {
      const p = pattern.trim().toLowerCase();
      if (!p) continue;
      if (p.endsWith("*")) {
        const prefix = p.slice(0, -1);
        if (lowerKey.startsWith(prefix)) return true;
      } else if (lowerKey === p) {
        return true;
      }
    }
  }
  return false;
}

export function isEnrichmentReadOnly(
  key: string,
  customReadOnly?: string[]
): boolean {
  const lowerKey = key.toLowerCase();
  const defaultReadOnly = [
    "ticket",
    "ticket_url",
    "jira",
    "jira_url",
    "mattermost",
    "mattermost_url",
    "runbook",
    "runbook_url",
    "cluster",
    "namespace",
    "zone",
    "service",
    "external_incident",
  ];
  if (defaultReadOnly.includes(lowerKey)) return true;

  if (customReadOnly && customReadOnly.length > 0) {
    for (const pattern of customReadOnly) {
      const p = pattern.trim().toLowerCase();
      if (!p) continue;
      if (p.endsWith("*")) {
        const prefix = p.slice(0, -1);
        if (lowerKey.startsWith(prefix)) return true;
      } else if (lowerKey === p) {
        return true;
      }
    }
  }
  return false;
}

interface ExternalLinkItem {
  id: string;
  title: string;
  url: string;
  provider?: string;
}

function extractExternalLinks(incident: IncidentDto): ExternalLinkItem[] {
  const links: ExternalLinkItem[] = [];
  const seenUrls = new Set<string>();
  const urlRegex = /(https?:\/\/[^\s]+)/;

  // 1. Primary external incident from enrichments (incident_url)
  if (incident.enrichments?.incident_url) {
    const rawUrl = String(incident.enrichments.incident_url).trim();
    if (rawUrl && !seenUrls.has(rawUrl)) {
      seenUrls.add(rawUrl);
      const provider =
        incident.enrichments?.incident_provider ||
        (rawUrl.includes("mattermost")
          ? "mattermost"
          : rawUrl.includes("jira")
          ? "jira"
          : undefined);
      links.push({
        id: "primary_external_incident",
        title:
          incident.enrichments?.incident_title ||
          incident.user_generated_name ||
          (provider ? `${startCase(provider)} Incident` : "External Incident"),
        url: rawUrl,
        provider,
      });
    }
  }

  // 2. Ticket / Jira enrichments
  const ticketEnrichment =
    incident.enrichments?.ticket_url ||
    incident.enrichments?.jira_url ||
    incident.enrichments?.ticket ||
    incident.enrichments?.jira;

  if (ticketEnrichment) {
    const match = String(ticketEnrichment).match(urlRegex);
    const url = match ? match[1] : String(ticketEnrichment).trim();
    if (url.startsWith("http") && !seenUrls.has(url)) {
      seenUrls.add(url);
      const rawTitle = String(ticketEnrichment)
        .replace(url, "")
        .replace(/^ticket:\s*/i, "")
        .replace(/[\[\]()]/g, "")
        .trim();
      links.push({
        id: "ticket",
        title: rawTitle || "Jira Ticket",
        url,
        provider: "jira",
      });
    }
  }

  // 3. Runbook enrichments
  const runbookEnrichment =
    incident.enrichments?.runbook_url || incident.enrichments?.runbook;
  if (runbookEnrichment) {
    const match = String(runbookEnrichment).match(urlRegex);
    const url = match ? match[1] : String(runbookEnrichment).trim();
    if (url.startsWith("http") && !seenUrls.has(url)) {
      seenUrls.add(url);
      links.push({
        id: "runbook",
        title: "Runbook",
        url,
      });
    }
  }

  // 4. Other custom URL enrichments
  if (incident.enrichments) {
    for (const [key, val] of Object.entries(incident.enrichments)) {
      if (
        [
          "incident_url",
          "ticket_url",
          "jira_url",
          "ticket",
          "jira",
          "runbook_url",
          "runbook",
        ].includes(key)
      ) {
        continue;
      }
      if (typeof val === "string" && (key.endsWith("_url") || key.endsWith("_link"))) {
        const match = val.match(urlRegex);
        const url = match ? match[1] : val.trim();
        if (url.startsWith("http") && !seenUrls.has(url)) {
          seenUrls.add(url);
          const provider =
            key.includes("mattermost")
              ? "mattermost"
              : key.includes("slack")
              ? "slack"
              : key.includes("jira")
              ? "jira"
              : key.includes("github")
              ? "github"
              : undefined;
          links.push({
            id: key,
            title: startCase(key.replace(/(_url|_link)$/, "")),
            url,
            provider,
          });
        }
      }
    }
  }

  return links;
}

interface Props {
  incident: IncidentDto;
}

function Summary({
  title,
  summary,
  collapsable,
  className,
  alerts,
  incident,
}: {
  title: string;
  summary: string;
  collapsable?: boolean;
  className?: string;
  alerts: AlertDto[];
  incident: IncidentDto;
}) {
  const [generatedSummary, setGeneratedSummary] = useState("");
  const { data: config } = useConfig();
  const { updateIncident } = useIncidentActions();
  const context = useCopilotContext();
  useCopilotReadable({
    description: "The incident alerts",
    value: alerts,
  });
  useCopilotReadable({
    description: "The incident title",
    value: incident.user_generated_name ?? incident.ai_generated_name,
  });
  useCopilotAction({
    name: "setGeneratedSummary",
    description: "Set the generated summary",
    parameters: [
      { name: "summary", type: "string", description: "The generated summary" },
    ],
    handler: async ({ summary }) => {
      await updateIncident(
        incident.id,
        {
          user_summary: summary,
        },
        true
      );
      setGeneratedSummary(summary);
    },
  });
  const task = new CopilotTask({
    instructions:
      "Generate a short concise summary of the incident based on the context of the alerts and the title of the incident. Don't repeat prompt.",
  });
  const [generatingSummary, setGeneratingSummary] = useState(false);
  const executeTask = async () => {
    setGeneratingSummary(true);
    await task.run(context);
    setGeneratingSummary(false);
  };

  const formatedSummary = (
    <div className="prose prose-slate max-w-2xl [&>p]:!my-1 [&>ul]:!my-1 [&>ol]:!my-1">
      <FormattedContent content={summary ?? generatedSummary} format="html" />
    </div>
  );

  if (collapsable) {
    return (
      <Disclosure as="div" className={clsx("space-y-1", className)}>
        <Disclosure.Button>
          {({ open }) => (
            <h4 className="text-gray-500 text-sm inline-flex justify-between items-center gap-1">
              <span>{title}</span>
              <IoChevronDown
                className={clsx({ "rotate-180": open }, "text-slate-400")}
              />
            </h4>
          )}
        </Disclosure.Button>

        <Disclosure.Panel as="div" className="space-y-2 relative">
          {formatedSummary}
        </Disclosure.Panel>
      </Disclosure>
    );
  }

  return (
    <div>
      {formatedSummary}
      {config?.KEEP_OSS_ONLY === false && <Button
        variant="secondary"
        onClick={executeTask}
        className="mt-2.5"
        disabled={generatingSummary || !config?.OPEN_AI_API_KEY_SET}
        loading={generatingSummary}
        icon={TbSparkles}
        size="xs"
        tooltip={
          !config?.OPEN_AI_API_KEY_SET
            ? "AI is not configured"
            : "Generate AI summary"
        }
      >
        AI Summary
      </Button>}
    </div>
  );
}

function MergedCallout({
  merged_into_incident_id,
  className,
}: {
  merged_into_incident_id: string;
  className?: string;
}) {
  const { data: merged_incident } = useIncident(merged_into_incident_id);

  if (!merged_incident) {
    return null;
  }

  return (
    <Callout
      // @ts-ignore
      title={
        <div>
          <p>This incident was merged into</p>
          <Link
            icon={() => (
              <StatusIcon className="!p-0" status={merged_incident.status} />
            )}
            href={`/incidents/${merged_incident?.id}`}
          >
            {getIncidentName(merged_incident)}
          </Link>
        </div>
      }
      color="purple"
      className={className}
    />
  );
}

export function IncidentOverview({ incident: initialIncidentData }: Props) {
  const router = useRouter();
  const { data: config } = useConfig();
  const overviewFields = config?.INCIDENT_OVERVIEW_FIELDS;
  const isFieldVisible = (fieldName: string) => {
    if (!overviewFields || overviewFields.length === 0) return true;
    return overviewFields.includes(fieldName);
  };

  const { data: fetchedIncident, mutate } = useIncident(
    initialIncidentData.id,
    {
      fallbackData: initialIncidentData,
      revalidateOnMount: false,
    }
  );
  const incident = fetchedIncident || initialIncidentData;
  const summary = incident.user_summary || incident.generated_summary;
  // Why do we have "null" in services?
  const notNullServices = incident.services.filter(
    (service) => service !== "null"
  );
  const { assignIncident } = useIncidentActions();
  const {
    data: alerts,
    isLoading: _alertsLoading,
    error: alertsError,
  } = useIncidentAlerts(incident.id, 20, 0);
  const environments =
    incident.enrichments.environments ||
    (Array.from(
      new Set(
        alerts?.items
          .filter(
            (alert) =>
              alert.environment &&
              alert.environment !== "undefined" &&
              alert.environment !== "default"
          )
          .map((alert) => alert.environment)
      )
    ) as Array<string>);
  const repositories =
    incident.enrichments.repositories ||
    (Array.from(
      new Set(
        alerts?.items
          .filter((alert) => (alert as any).repository)
          .map((alert) => (alert as any).repository as string)
      )
    ) as Array<string>);

  const externalLinks = extractExternalLinks(incident);

  const filterBy = (key: string, value: string) => {
    router.push(
      `/alerts/feed?cel=${key}%3D%3D${encodeURIComponent(`"${value}"`)}`
    );
  };

  const api = useApi();

  const handleBulkEnrichmentChange = async (
    fields: Record<string, string | string[]>
  ) => {
    try {
      const requestData = {
        enrichments: fields,
        fingerprint: incident.id,
      };
      await api.post(`/incidents/${incident.id}/enrich`, requestData);
      await mutate();
    } catch (error) {
      // Handle unexpected error
      console.error("An unexpected error occurred");
    }
  };

  const handleBulkUnEnrichment = async (fields: string[]) => {
    try {
      const requestData = {
        enrichments: fields,
        fingerprint: incident.id,
      };
      await api.post(`/incidents/${incident.id}/unenrich`, requestData);
      await mutate();
    } catch (error) {
      // Handle unexpected error
      console.error("An unexpected error occurred");
    }
  };

  const handleEnrichmentChange = async (
    fieldName: string,
    fieldValue: string | string[]
  ) => {
    await handleBulkEnrichmentChange({ [fieldName]: fieldValue });
  };

  const handleUnEnrichment = async (fieldName: string) => {
    await handleBulkUnEnrichment([fieldName]);
  };

  if (!alerts || _alertsLoading) {
    return <IncidentOverviewSkeleton />;
  }
  return (
    // Adding padding bottom to visually separate from the tabs
    <div className="flex gap-6 items-start w-full text-tremor-default">
      <div className="basis-2/3 grow">
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
          <div className="max-w-2xl">
            <FieldHeader>Summary</FieldHeader>
            <Summary
              title="Summary"
              summary={summary}
              alerts={alerts.items}
              incident={incident}
            />
            {/* @tb: not sure how we use this, but leaving it here for now
            {incident.user_summary && incident.generated_summary ? (
              <Summary
                title="AI version"
                summary={incident.generated_summary}
                collapsable={true}
                alerts={alerts.items}
                incident={incident}
              />
            ) : null} */}
            {incident.merged_into_incident_id && (
              <MergedCallout
                className="inline-block mt-2"
                merged_into_incident_id={incident.merged_into_incident_id}
              />
            )}
            {isFieldVisible("same_incident_in_the_past") && !!incident.same_incident_in_the_past_id && (
              <div className="mt-2">
                <SameIncidentField incident={incident} />
              </div>
            )}
          </div>
          <div className="flex flex-col gap-2">
            <div className="grid grid-cols-2 gap-4">
              {isFieldVisible("services") && notNullServices.length > 0 && (
                <div>
                  <FieldHeader>Services</FieldHeader>
                  <EnrichmentEditableField
                    name="services"
                    value={notNullServices}
                    onUpdate={handleEnrichmentChange}
                    onDelete={
                      incident.enrichments?.services
                        ? handleUnEnrichment
                        : undefined
                    }
                  />
                </div>
              )}

              {isFieldVisible("environments") && environments && environments.length > 0 && (
                <div>
                  <FieldHeader>Environments</FieldHeader>
                  <EnrichmentEditableField
                    name="environments"
                    value={environments}
                    onUpdate={handleEnrichmentChange}
                    onDelete={
                      incident.enrichments?.environments
                        ? handleUnEnrichment
                        : undefined
                    }
                  />
                </div>
              )}

              {isFieldVisible("external_incident") && (
                <div>
                  <FieldHeader>External links</FieldHeader>

                  <EnrichmentEditableForm
                    fields={{
                      incident_id: incident.enrichments?.incident_id,
                      incident_url: incident.enrichments?.incident_url,
                      incident_provider: incident.enrichments?.incident_provider,
                      incident_title: incident.enrichments?.incident_title,
                    }}
                    title="External incident"
                    onUpdate={handleBulkEnrichmentChange}
                    onDelete={handleBulkUnEnrichment}
                    readOnly={isEnrichmentReadOnly(
                      "external_incident",
                      config?.ENRICHMENTS_READ_ONLY_KEYS
                    )}
                  >
                    <>
                      {externalLinks.length > 0 ? (
                        <div className="flex flex-wrap gap-1.5 items-center">
                          {externalLinks.map((link) => (
                            <Badge
                              key={link.id}
                              size="sm"
                              color="orange"
                              icon={
                                link.provider
                                  ? (props: any) => (
                                      <DynamicImageProviderIcon
                                        providerType={link.provider}
                                        src={`/icons/${link.provider}-icon.png`}
                                        height="20"
                                        width="20"
                                        {...props}
                                      />
                                    )
                                  : (props: any) => (
                                      <FiExternalLink
                                        className="w-3.5 h-3.5 text-orange-600 inline"
                                        {...props}
                                      />
                                    )
                              }
                              className="cursor-pointer inline-flex items-center gap-1 hover:bg-orange-200 transition-colors"
                              tooltip={link.url}
                              onClick={() => window.open(link.url, "_blank")}
                            >
                              <span className="truncate max-w-[200px]">
                                {link.title}
                              </span>
                            </Badge>
                          ))}
                        </div>
                      ) : (
                        "No external links"
                      )}
                    </>
                  </EnrichmentEditableForm>
                </div>
              )}

              {isFieldVisible("repositories") && repositories && repositories.length > 0 && (
                <div>
                  <FieldHeader>Repositories</FieldHeader>

                  <EnrichmentEditableField
                    name="repositories"
                    value={repositories}
                    onUpdate={handleEnrichmentChange}
                    onDelete={
                      incident.enrichments?.repositories
                        ? handleUnEnrichment
                        : undefined
                    }
                  >
                    {repositories?.length > 0 ? (
                      <div className="flex flex-wrap gap-1">
                        {repositories.map((repo: any) => {
                          const repoName = repo.split("/").pop();
                          return (
                            <Badge
                              key={repo}
                              color="orange"
                              size="sm"
                              icon={(props: any) => (
                                <DynamicImageProviderIcon
                                  providerType="github"
                                  src={`/icons/github-icon.png`}
                                  height="24"
                                  width="24"
                                  {...props}
                                />
                              )}
                              className="cursor-pointer"
                              onClick={() => window.open(repo, "_blank")}
                            >
                              {repoName}
                            </Badge>
                          );
                        })}
                      </div>
                    ) : (
                      "No environments involved"
                    )}
                  </EnrichmentEditableField>
                </div>
              )}

              {isFieldVisible("assignee") && (
                <div>
                  <FieldHeader>Assignee</FieldHeader>
                  <div className="flex flex-col gap-1">
                    {incident.assignee ? (
                      <p>{incident.assignee}</p>
                    ) : (
                      <p>No assignee yet</p>
                    )}
                    <div>
                      <span
                        className="text-sm text-gray-500 cursor-pointer hover:text-orange-500 underline"
                        onClick={() => {
                          if (
                            confirm(
                              "Are you sure you want to assign this incident to yourself?"
                            )
                          ) {
                            assignIncident(incident.id);
                          }
                        }}
                      >
                        Assign to me
                      </span>
                    </div>
                  </div>
                </div>
              )}

              {isFieldVisible("grouped_by") &&
                incident.rule_fingerprint !== "none" &&
                !!incident.rule_fingerprint && (
                  <div>
                    <FieldHeader>Grouped by</FieldHeader>
                    <div className="flex flex-wrap gap-1">
                      <Badge
                        color="orange"
                        size="sm"
                        className="cursor-pointer overflow-ellipsis"
                        tooltip={incident.rule_fingerprint}
                      >
                        {incident.rule_fingerprint.length > 10
                          ? incident.rule_fingerprint.slice(0, 10) + "..."
                          : incident.rule_fingerprint}
                      </Badge>
                    </div>
                  </div>
                )}
              {isFieldVisible("enrichments") && (
                <>
                  {map(incident.enrichments, (value: any, key: string) => {
                    if (isEnrichmentHidden(key, config?.ENRICHMENTS_HIDDEN_KEYS))
                      return;
                    const isReadOnly = isEnrichmentReadOnly(
                      key,
                      config?.ENRICHMENTS_READ_ONLY_KEYS
                    );
                    return (
                      <div key={`incident-enrichment-${key}`}>
                        <FieldHeader>{startCase(key)}</FieldHeader>
                        <EnrichmentEditableField
                          name={key}
                          value={value}
                          onUpdate={handleEnrichmentChange}
                          onDelete={handleUnEnrichment}
                          readOnly={isReadOnly}
                        />
                      </div>
                    );
                  })}
                  <div>
                    <EnrichmentEditableField
                      value={""}
                      onUpdate={handleEnrichmentChange}
                    />
                  </div>
                </>
              )}
            </div>
          </div>
          <div>
            <FollowingIncidents incident={incident} />
          </div>
        </div>
      </div>
      <div className="pr-10 grid grid-cols-1 xl:grid-cols-2 gap-4">
        <div>
          <FieldHeader>Status</FieldHeader>
          <IncidentChangeStatusSelect
            incidentId={incident.id}
            value={incident.status}
          />
        </div>
        <div>
          <FieldHeader>Severity</FieldHeader>
          <IncidentChangeSeveritySelect
            incidentId={incident.id}
            value={incident.severity}
          />
        </div>
        {!!incident.last_seen_time && (
          <div>
            <FieldHeader>Last seen at</FieldHeader>
            <DateTimeField date={incident.last_seen_time} />
          </div>
        )}
        {!!incident.start_time && (
          <div>
            <FieldHeader>Started at</FieldHeader>
            <DateTimeField date={incident.start_time} />
          </div>
        )}
        {incident?.enrichments && "rca_points" in incident.enrichments && (
          <RootCauseAnalysis points={incident.enrichments.rca_points} />
        )}
        <div>
          <FieldHeader>Resolve on</FieldHeader>
          <Badge
            size="sm"
            color="orange"
            className="cursor-help"
            tooltip={
              incident.resolve_on === "all_resolved"
                ? "Incident will be resolved when all its alerts are resolved"
                : "Incident will resolve only when manually set to resolved"
            }
          >
            {incident.resolve_on}
          </Badge>
        </div>
      </div>
    </div>
  );
}
