// Browser error telemetry. Off unless the server's /api/v1/client-config names a DSN; then the SDK chunk
// (bundled from npm, never a CDN) loads and sends errors to the exact ingest host the CSP allows.
//
// What is sent: exception types, stack frames (no query or fragment), a page name, the numeric user id,
// release and environment. What is never sent: URLs, request data, breadcrumbs, console output, DOM,
// component props or state, and exception messages unless they match a short list of known-safe shapes.
import { apiUrl } from "./api.js";

let sentry = null;
let pending = null;
let userId = null;
let page = null;

// Messages kept verbatim; anything else becomes "[redacted]" (it could quote a resume, a posting or a field).
const SAFE_MESSAGES = [
  /^Cannot read properties of (undefined|null) \(reading '[A-Za-z_$][\w$]{0,40}'\)$/,
  /^[A-Za-z_$][\w$.]{0,60} is not a function$/,
  /^[A-Za-z_$][\w$]{0,40} is not defined$/,
  /^Failed to fetch$/,
  /^NetworkError when attempting to fetch resource\.$/,
  /^Load failed$/,
  /^Loading chunk \d+ failed\.?$/,
];
const DYNAMIC_IMPORT = /^Failed to fetch dynamically imported module\b/;
const REACT_ERROR = /^Minified React error #(\d+)\b/;
const PAGE = /^[a-z][a-z-]{0,30}$/;
const DIGITS = /^\d{1,12}$/;

export function safeMessage(value) {
  if (typeof value !== "string") return "[redacted]";
  const text = value.trim();
  if (text.length > 120 && !DYNAMIC_IMPORT.test(text) && !REACT_ERROR.test(text)) return "[redacted]";
  if (SAFE_MESSAGES.some((shape) => shape.test(text))) return text;
  if (DYNAMIC_IMPORT.test(text)) return "Failed to fetch dynamically imported module";
  const react = REACT_ERROR.exec(text);
  if (react) return `Minified React error #${react[1]}`;
  return "[redacted]";
}

const stripUrl = (value) => (typeof value === "string" ? value.split(/[?#]/)[0] : value);

function frames(stacktrace) {
  return {
    frames: (stacktrace?.frames || []).map((frame) => {
      const kept = {};
      for (const key of ["function", "lineno", "colno", "in_app"]) if (key in frame) kept[key] = frame[key];
      if (frame.filename) kept.filename = stripUrl(frame.filename);
      if (frame.abs_path) kept.abs_path = stripUrl(frame.abs_path);
      return kept;
    }),
  };
}

// Rebuild the event from an allowlist. Exported for the browser test's unit checks.
export function scrub(event) {
  try {
    const out = {};
    for (const key of ["event_id", "timestamp", "level", "platform", "release", "environment", "sdk"]) {
      if (key in event) out[key] = event[key];
    }
    if (event.exception?.values) {
      out.exception = {
        values: event.exception.values.map((value) => ({
          type: typeof value.type === "string" ? value.type.slice(0, 80) : "Error",
          value: safeMessage(value.value),
          ...(value.mechanism ? { mechanism: { type: value.mechanism.type, handled: value.mechanism.handled } } : {}),
          ...(value.stacktrace ? { stacktrace: frames(value.stacktrace) } : {}),
        })),
      };
    }
    const tags = { service: "frontend" };
    if (page && PAGE.test(page)) tags.page = page;
    out.tags = tags;
    if (userId !== null && DIGITS.test(String(userId))) out.user = { id: String(userId) };
    const contexts = {};
    for (const name of ["browser", "os"]) {
      const context = event.contexts?.[name];
      if (context) contexts[name] = { name: context.name, version: context.version };
    }
    out.contexts = contexts;
    return out;
  } catch {
    return null; // never send an event we could not scrub
  }
}

async function load() {
  const response = await fetch(apiUrl("/client-config"), { credentials: "same-origin" });
  if (!response.ok) return null;
  const config = (await response.json())?.sentry;
  if (!config?.dsn) return null;
  const { start } = await import("./sentryClient.js");
  sentry = start(config, scrub);
  return sentry;
}

// Called once at startup, after the app has read (and stripped) any token in the address bar.
export function initTelemetry() {
  if (!pending) pending = load().catch(() => null);
  return pending;
}

export function setTelemetryUser(id) {
  userId = id ?? null;
}

export function setTelemetryPage(name) {
  page = name ?? null;
}

// For error boundaries: sends only the error (never props, state or component stacks). Returns the event id.
export function reportError(error) {
  if (!sentry) return null;
  try {
    return sentry.captureException(error);
  } catch {
    return null;
  }
}
