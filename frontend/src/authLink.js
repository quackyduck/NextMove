// Emailed links carry a secret in the URL fragment: /verify-email#token=… and /reset-password#token=….
// This module is the FIRST import in main.jsx, so it runs before React, error reporting or any other
// script: it copies the token into module memory and removes the fragment from the address bar at once,
// so no later code, history entry, error report or screenshot sees it. It never touches storage.
const ROUTES = { "verify-email": "verify", "reset-password": "reset" };
let captured = null;

export function captureLinkToken(location = window.location, history = window.history) {
  const base = import.meta.env.BASE_URL || "/";
  const route = location.pathname.startsWith(base) ? location.pathname.slice(base.length).replace(/\/$/, "") : "";
  const kind = ROUTES[route];
  if (!kind || !location.hash) return null;
  const token = new URLSearchParams(location.hash.slice(1)).get("token") || "";
  // Strip the fragment whatever it held, before anything else can read it.
  history.replaceState(null, "", location.pathname + location.search);
  captured = /^[A-Za-z0-9_-]{16,128}$/.test(token) ? { kind, token } : { kind, token: "" };
  return captured;
}

// The link page for `kind` ("verify" or "reset") takes the token once; later calls get "".
export function takeLinkToken(kind) {
  if (!captured || captured.kind !== kind) return "";
  const { token } = captured;
  captured = { kind, token: "" };
  return token;
}

// Which link page this load is for ("verify", "reset"), or null, even after the token was taken.
export function linkRoute(location = window.location) {
  const base = import.meta.env.BASE_URL || "/";
  const route = location.pathname.startsWith(base) ? location.pathname.slice(base.length).replace(/\/$/, "") : "";
  return ROUTES[route] || null;
}

captureLinkToken();
