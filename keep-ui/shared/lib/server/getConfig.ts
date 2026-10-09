import { InternalConfig } from "@/types/internal-config";
import { getApiURL } from "@/utils/apiUrl";
import {
  AuthType,
  MULTI_TENANT,
  NO_AUTH,
  SINGLE_TENANT,
} from "@/utils/authenticationType";

export function getConfig(): InternalConfig {
  let authType = process.env.AUTH_TYPE;
  // Backward compatibility
  if (authType === MULTI_TENANT) {
    authType = AuthType.AUTH0;
  } else if (authType === SINGLE_TENANT) {
    authType = AuthType.DB;
  } else if (authType === NO_AUTH) {
    authType = AuthType.NOAUTH;
  } else if (Object.values(AuthType).includes(authType as AuthType)) {
    // Keep the auth type if it's a valid enum value
    authType = authType as AuthType;
  } else {
    // Default to NOAUTH
    authType = AuthType.NOAUTH;
  }

  // we want to support preview branches on vercel
  let API_URL_CLIENT;
  // if we are on vercel, default to getApiURL() if no API_URL_CLIENT is set
  if (process.env.VERCEL_GIT_COMMIT_REF) {
    API_URL_CLIENT = process.env.API_URL_CLIENT || getApiURL();
    // else, no default since we will use relative URLs
  } else {
    API_URL_CLIENT = process.env.API_URL_CLIENT;
  }

  // Parse alert sidebar fields from environment variable
  // Default includes all standard fields
  const defaultAlertSidebarFields = [
    "service",
    "source",
    "description",
    "message",
    "fingerprint",
    "url",
    "incidents",
    "timeline",
    "relatedServices",
  ];
  const alertSidebarFields = process.env.ALERT_SIDEBAR_FIELDS
    ? process.env.ALERT_SIDEBAR_FIELDS.split(",").map((field) => field.trim())
    : defaultAlertSidebarFields;

  const defaultIncidentsStatusFilter = process.env.DEFAULT_INCIDENTS_STATUS_FILTER
    ? process.env.DEFAULT_INCIDENTS_STATUS_FILTER.split(",").map((s) => s.trim())
    : ["firing", "acknowledged"];

  const defaultFeedStatusFilter = process.env.DEFAULT_FEED_STATUS_FILTER
    ? process.env.DEFAULT_FEED_STATUS_FILTER.split(",").map((s) => s.trim())
    : ["firing", "acknowledged", "suppressed", "pending"];

  const defaultFacetsOrder = process.env.DEFAULT_FACETS_ORDER
    ? process.env.DEFAULT_FACETS_ORDER.split(",").map((s) => s.trim())
    : ["Zone", "Cluster", "Namespace"];

  const defaultOpenFacets = process.env.DEFAULT_OPEN_FACETS
    ? process.env.DEFAULT_OPEN_FACETS.split(",").map((s) => s.trim())
    : [];

  const incidentTableColumns = process.env.INCIDENT_TABLE_COLUMNS
    ? process.env.INCIDENT_TABLE_COLUMNS.split(",").map((s) => s.trim())
    : [
        "severity",
        "selected",
        "status",
        "name",
        "cluster",
        "alerts_count",
        "alert_sources",
        "creation_time",
        "actions",
      ];

  const incidentOverviewFields = process.env.INCIDENT_OVERVIEW_FIELDS
    ? process.env.INCIDENT_OVERVIEW_FIELDS.split(",").map((s) => s.trim())
    : [
        "summary",
        "external_incident",
        "grouped_by",
        "services",
        "environments",
        "repositories",
        "enrichments",
      ];

  const incidentAlertsColumns = process.env.INCIDENT_ALERTS_COLUMNS
    ? process.env.INCIDENT_ALERTS_COLUMNS.split(",").map((s) => s.trim())
    : ["cluster", "namespace", "level"];

  const enrichmentsHiddenKeys = process.env.ENRICHMENTS_HIDDEN_KEYS
    ? process.env.ENRICHMENTS_HIDDEN_KEYS.split(",").map((s) => s.trim())
    : ["mm_*", "snooze_*", "mm *", "snooze *", "_*"];

  const enrichmentsReadOnlyKeys = process.env.ENRICHMENTS_READ_ONLY_KEYS
    ? process.env.ENRICHMENTS_READ_ONLY_KEYS.split(",").map((s) => s.trim())
    : [
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

  const defaultPresetTagsOrder = process.env.DEFAULT_PRESET_TAGS_ORDER
    ? process.env.DEFAULT_PRESET_TAGS_ORDER.split(",").map((s) => s.trim())
    : [];

  return {
    AUTH_TYPE: authType,
    KEEP_OSS_ONLY: process.env.KEEP_OSS_ONLY !== "false",
    PUSHER_DISABLED: process.env.PUSHER_DISABLED === "true",
    // could be relative (for ingress) or absolute (e.g. Pusher)
    PUSHER_HOST: process.env.PUSHER_HOST,
    PUSHER_PORT: process.env.PUSHER_HOST
      ? parseInt(process.env.PUSHER_PORT!)
      : undefined,
    PUSHER_APP_KEY: process.env.PUSHER_APP_KEY,
    PUSHER_CLUSTER: process.env.PUSHER_CLUSTER,
    // The API URL is used by the server to make requests to the API
    //   note that we need two different URLs for the client and the server
    //   because in some environments, e.g. docker-compose, the server can get keep-backend
    //   whereas the client (browser) can get only localhost
    API_URL: process.env.API_URL,
    // could be relative (e.g. for ingress) or absolute (e.g. for cloud run)
    API_URL_CLIENT: API_URL_CLIENT,
    POSTHOG_KEY: process.env.POSTHOG_KEY,
    POSTHOG_DISABLED: process.env.POSTHOG_DISABLED || "true",
    POSTHOG_HOST: process.env.POSTHOG_HOST,
    SENTRY_DISABLED: process.env.SENTRY_DISABLED || "true",
    READ_ONLY: process.env.KEEP_READ_ONLY === "true",
    OPEN_AI_API_KEY_SET:
      !!process.env.OPEN_AI_API_KEY || !!process.env.OPENAI_API_KEY,
    // NOISY ALERTS DISABLED BY DEFAULT TO SPARE SPACE ON THE TABLE
    NOISY_ALERTS_ENABLED: process.env.NOISY_ALERTS_ENABLED === "true",
    // The URL of the documentation site
    KEEP_DOCS_URL: process.env.KEEP_DOCS_URL || "https://docs.keephq.dev",
    KEEP_CONTACT_US_URL:
      process.env.KEEP_CONTACT_US_URL || "https://slack.keephq.dev/",
    KEEP_HIDE_SENSITIVE_FIELDS:
      process.env.KEEP_HIDE_SENSITIVE_FIELDS === "true",
    KEEP_WORKFLOW_DEBUG: process.env.KEEP_WORKFLOW_DEBUG === "true",
    HIDE_NAVBAR_DEDUPLICATION:
      process.env.HIDE_NAVBAR_DEDUPLICATION?.toLowerCase() === "true",
    HIDE_NAVBAR_WORKFLOWS:
      process.env.HIDE_NAVBAR_WORKFLOWS?.toLowerCase() === "true",
    HIDE_NAVBAR_SERVICE_TOPOLOGY:
      process.env.HIDE_NAVBAR_SERVICE_TOPOLOGY?.toLowerCase() === "true",
    HIDE_NAVBAR_MAPPING:
      process.env.HIDE_NAVBAR_MAPPING?.toLowerCase() === "true",
    HIDE_NAVBAR_EXTRACTION:
      process.env.HIDE_NAVBAR_EXTRACTION?.toLowerCase() === "true",
    HIDE_NAVBAR_MAINTENANCE_WINDOW:
      process.env.HIDE_NAVBAR_MAINTENANCE_WINDOW?.toLowerCase() === "true",
    HIDE_NAVBAR_AI_PLUGINS:
      process.env.HIDE_NAVBAR_AI_PLUGINS?.toLowerCase() === "true",
    // Ticketing integration
    KEEP_TICKETING_ENABLED:
      process.env.KEEP_TICKETING_ENABLED?.toLowerCase() === "true",
    KEEP_WF_LIST_EXTENDED_INFO:
      process.env.KEEP_WF_LIST_EXTENDED_INFO?.toLowerCase() === "true",
    // Alert sidebar fields configuration
    ALERT_SIDEBAR_FIELDS: alertSidebarFields,
    DEFAULT_INCIDENTS_STATUS_FILTER: defaultIncidentsStatusFilter,
    DEFAULT_FEED_STATUS_FILTER: defaultFeedStatusFilter,
    DEFAULT_FACETS_ORDER: defaultFacetsOrder,
    DEFAULT_OPEN_FACETS: defaultOpenFacets,
    INCIDENT_TABLE_COLUMNS: incidentTableColumns,
    INCIDENT_OVERVIEW_FIELDS: incidentOverviewFields,
    INCIDENT_ALERTS_COLUMNS: incidentAlertsColumns,
    ENRICHMENTS_HIDDEN_KEYS: enrichmentsHiddenKeys,
    ENRICHMENTS_READ_ONLY_KEYS: enrichmentsReadOnlyKeys,
    DEFAULT_PRESET_TAGS_ORDER: defaultPresetTagsOrder,
  };
}
