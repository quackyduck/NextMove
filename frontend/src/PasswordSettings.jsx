import React, { useEffect, useId, useRef, useState } from "react";
import { api, setCsrf } from "./api.js";

export function PasswordSettings({ onClose, onSessionExpired }) {
  const dialog = useRef(null);
  const id = useId();
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [needsSignIn, setNeedsSignIn] = useState(false);

  useEffect(() => {
    dialog.current.showModal();
  }, []);
  async function update(event) {
    event.preventDefault();
    setError("");
    if (newPassword !== confirmation) {
      setError("New passwords do not match");
      return;
    }
    setBusy(true);
    try {
      const data = await api("/auth/password", {
        method: "POST",
        body: {
          current_password: currentPassword,
          new_password: newPassword,
          confirm_password: confirmation,
        },
      });
      setCsrf(data.csrf_token);
      setCurrentPassword("");
      setNewPassword("");
      setConfirmation("");
      setSaved(true);
    } catch (error) {
      setError(error.message);
      if (error.status === 401 || error.status === 409) setNeedsSignIn(true);
    } finally {
      setBusy(false);
    }
  }
  return (
    <dialog
      ref={dialog}
      className="password-settings"
      aria-labelledby={`${id}-title`}
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) onClose();
      }}
    >
      <h2 id={`${id}-title`}>Change password</h2>
      {saved ? (
        <>
          <p role="status" className="alert alert-success">
            Password updated. Other sessions have been signed out.
          </p>
          <p>You’re still signed in on this device.</p>
          <button className="btn btn-primary" onClick={onClose}>
            Done
          </button>
        </>
      ) : (
        <form onSubmit={update}>
          <p>
            Use 8–128 characters. Other sessions will be signed out; this one
            stays signed in.
          </p>
          <label className="form-label" htmlFor={`${id}-current`}>
            Current password
          </label>
          <input
            className="form-control mb-3"
            id={`${id}-current`}
            type="password"
            autoComplete="current-password"
            value={currentPassword}
            onChange={(e) => setCurrentPassword(e.target.value)}
            maxLength={128}
            required
            autoFocus
          />
          <label className="form-label" htmlFor={`${id}-new`}>
            New password
          </label>
          <input
            className="form-control mb-3"
            id={`${id}-new`}
            type="password"
            autoComplete="new-password"
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            minLength={8}
            maxLength={128}
            required
          />
          <label className="form-label" htmlFor={`${id}-confirm`}>
            Confirm new password
          </label>
          <input
            className="form-control mb-3"
            id={`${id}-confirm`}
            type="password"
            autoComplete="new-password"
            value={confirmation}
            onChange={(e) => setConfirmation(e.target.value)}
            minLength={8}
            maxLength={128}
            required
          />
          {error && (
            <p role="alert" className="alert alert-danger">
              {error}
            </p>
          )}
          <div className="d-flex flex-wrap gap-2">
            {needsSignIn ? (
              <button
                type="button"
                className="btn btn-primary"
                onClick={onSessionExpired}
              >
                Return to sign in
              </button>
            ) : (
              <button className="btn btn-primary" disabled={busy}>
                {busy ? "Updating…" : "Update password"}
              </button>
            )}
            <button
              type="button"
              className="btn btn-outline-secondary"
              disabled={busy}
              onClick={onClose}
            >
              Cancel
            </button>
          </div>
        </form>
      )}
    </dialog>
  );
}
