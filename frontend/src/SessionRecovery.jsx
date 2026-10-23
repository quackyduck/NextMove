import React, { useEffect, useId, useRef, useState } from "react";
import { api, setCsrf } from "./api.js";

// A modal keeps the workspace mounted, including unsaved text and file inputs.
export function SessionRecovery({ onAuthenticated }) {
  const dialog = useRef(null);
  const titleId = useId();
  const passwordId = useId();
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    dialog.current.showModal();
    const warnBeforeReload = (event) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warnBeforeReload);
    return () => window.removeEventListener("beforeunload", warnBeforeReload);
  }, []);

  async function signIn(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const data = await api("/auth/login", {
        method: "POST",
        body: { password },
      });
      setCsrf(data.csrf_token);
      setPassword("");
      onAuthenticated();
    } catch (error) {
      setError(error.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <dialog
      ref={dialog}
      className="session-recovery"
      aria-labelledby={titleId}
      onCancel={(event) => event.preventDefault()}
    >
      <h2 id={titleId}>Sign in again</h2>
      <p>
        Your session expired or changed. Your unsaved edits and selected files
        are still here in this tab. Sign in, then retry the action you were
        taking.
      </p>
      <form onSubmit={signIn}>
        <label htmlFor={passwordId} className="form-label">
          Workspace password
        </label>
        <input
          id={passwordId}
          type="password"
          autoComplete="current-password"
          className="form-control"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          required
          autoFocus
        />
        {error && (
          <p role="alert" className="alert alert-danger mt-3">
            {error}
          </p>
        )}
        <button className="btn btn-primary w-100 mt-3" disabled={busy}>
          {busy ? "Signing in…" : "Sign back in"}
        </button>
      </form>
    </dialog>
  );
}
