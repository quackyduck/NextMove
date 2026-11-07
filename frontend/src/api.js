let csrf = null;
const REQUEST_ID = /^[A-Za-z0-9-]{8,64}$/;
let authenticationGeneration = 0;
const authenticationListeners = new Set();
export function onAuthenticationRequired(listener) {
  authenticationListeners.add(listener);
  return () => authenticationListeners.delete(listener);
}
export const apiUrl = (path) =>
  `${import.meta.env.BASE_URL.replace(/\/$/, "")}/api/v1${path}`;
export async function api(path, { method = "GET", body, ...options } = {}) {
  const requestGeneration = authenticationGeneration;
  const headers = {};
  if (csrf) headers["X-CSRF-Token"] = csrf;
  if (body && !(body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(body);
  }
  const response = await fetch(apiUrl(path), {
    method,
    body,
    headers,
    credentials: "same-origin",
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    // A server failure names a reference the user can quote to support (the request's X-Request-ID).
    const candidate = data.request_id || response.headers.get("X-Request-ID");
    const requestId = REQUEST_ID.test(candidate || "") ? candidate : null;
    let message = data.error || `Request failed (${response.status})`;
    if (response.status >= 500 && requestId) message += ` (reference: ${requestId})`;
    const error = new Error(message);
    error.status = response.status;
    error.requestId = requestId;
    error.data = data;
    error.authenticationRequired =
      !path.startsWith("/auth/") &&
      (response.status === 401 ||
        (response.status === 403 && data.error === "Invalid CSRF token"));
    // A response from the previous session must not reopen recovery after login.
    if (
      error.authenticationRequired &&
      requestGeneration === authenticationGeneration
    ) {
      csrf = null;
      authenticationListeners.forEach((listener) => listener());
    }
    throw error;
  }
  return data;
}

export function setCsrf(value) {
  csrf = value;
  authenticationGeneration += 1;
}
