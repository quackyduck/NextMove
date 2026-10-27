let csrf = null;
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
    const error = new Error(
      data.error || `Request failed (${response.status})`,
    );
    error.status = response.status;
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
