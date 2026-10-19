let csrf = null;
export const apiUrl = (path) => `${import.meta.env.BASE_URL.replace(/\/$/, "")}/api/v1${path}`;
export async function api(path, { method = "GET", body, ...options } = {}) {
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
    throw error;
  }
  return data;
}

export function setCsrf(value) {
  csrf = value;
}
