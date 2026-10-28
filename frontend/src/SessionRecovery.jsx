import React, { useEffect, useId, useRef, useState } from "react";
import { api, setCsrf } from "./api.js";

// A modal keeps the workspace mounted, including unsaved text and file inputs.
export function SessionRecovery({ email: knownEmail = "", onAuthenticated, onSwitchAccount }) {
  const dialog = useRef(null);
  const titleId = useId();
  const emailId = useId();
  const passwordId = useId();
  const [email, setEmail] = useState(knownEmail);
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const warnBeforeReload = useRef((event) => {
    event.preventDefault();
    event.returnValue = "";
  });

  useEffect(() => {
    dialog.current.showModal();
    const warn = warnBeforeReload.current;
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, []);

  async function signIn(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const data = await api("/auth/login", {
        method: "POST",
        body: { email, password },
      });
      setCsrf(data.csrf_token);
      setPassword("");
      // Signed in: the app may now reload (e.g. a different account signed in).
      window.removeEventListener("beforeunload", warnBeforeReload.current);
      onAuthenticated(data);
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
        <label htmlFor={emailId} className="form-label">
          Email
        </label>
        <input
          id={emailId}
          type="email"
          autoComplete="username"
          className="form-control"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          required
          autoFocus={!email}
        />
        <label htmlFor={passwordId} className="form-label mt-3">
          Password
        </label>
        <input
          id={passwordId}
          type="password"
          autoComplete="current-password"
          className="form-control"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          required
          autoFocus={Boolean(email)}
        />
        {error && (
          <p role="alert" className="alert alert-danger mt-3">
            {error}
          </p>
        )}
        <button className="btn btn-primary w-100 mt-3" disabled={busy}>
          {busy ? "Signing in…" : "Sign back in"}
        </button>
        {onSwitchAccount && (
          <button
            type="button"
            className="btn btn-link w-100 mt-2"
            disabled={busy}
            onClick={onSwitchAccount}
          >
            Sign in as someone else (discards unsaved edits)
          </button>
        )}
      </form>
    </dialog>
  );
}
