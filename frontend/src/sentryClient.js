// Loaded only when browser telemetry is on (telemetry.js imports this module dynamically). Named imports
// let the bundler drop everything else in @sentry/browser: there is no Replay, Feedback or tracing code here.
import {
  captureException,
  dedupeIntegration,
  eventFiltersIntegration,
  globalHandlersIntegration,
  init,
  linkedErrorsIntegration,
} from "@sentry/browser";

export function start(config, scrub) {
  init({
    dsn: config.dsn,
    environment: config.environment || undefined,
    sendDefaultPii: false,
    defaultIntegrations: false,
    integrations: [eventFiltersIntegration(), globalHandlersIntegration(), linkedErrorsIntegration(), dedupeIntegration()],
    maxBreadcrumbs: 0,
    beforeBreadcrumb: () => null,
    beforeSend: scrub,
    beforeSendTransaction: () => null,
    tracePropagationTargets: [],
    sendClientReports: false,
  });
  return { captureException };
}
