// One confirmation dialog for every permanent deletion (roles, resumes, template versions, templates).
// Focus starts on Cancel; Escape cancels; an optional typed confirmation guards the biggest deletions.
import React, { useEffect, useId, useRef, useState } from "react";

export function DeleteDialog({ title, children, confirmText = "", confirmLabel = "", onConfirm, onClose }) {
  const id = useId();
  const dialog = useRef(null);
  const cancel = useRef(null);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    const node = dialog.current;
    node.showModal();
    cancel.current?.focus();
    return () => node.open && node.close();
  }, []);
  const ready = !confirmText || typed.trim() === confirmText;
  async function confirm() {
    setBusy(true);
    setError("");
    try {
      await onConfirm();
      onClose(true);
    } catch (failure) {
      setError(failure.message);
      setBusy(false);
    }
  }
  return (
    <dialog ref={dialog} className="delete-dialog" role="alertdialog" aria-modal="true"
      aria-labelledby={`${id}-title`} aria-describedby={`${id}-body`}
      onCancel={(event) => { event.preventDefault(); if (!busy) onClose(false); }}>
      <h2 id={`${id}-title`} className="h4 mt-0">{title}</h2>
      <div id={`${id}-body`}>{children}</div>
      <p className="mb-2"><strong>This can't be undone.</strong></p>
      {confirmText && (
        <div className="mb-3">
          <label htmlFor={`${id}-typed`} className="form-label">{confirmLabel || <>Type <bdi>{confirmText}</bdi> to confirm</>}</label>
          <input id={`${id}-typed`} className="form-control" autoComplete="off" spellCheck={false} value={typed}
            onChange={(event) => setTyped(event.target.value)} disabled={busy} />
        </div>
      )}
      {error && <p role="alert" className="alert alert-danger">{error}</p>}
      <div className="d-flex flex-wrap gap-2 justify-content-end">
        <button type="button" ref={cancel} className="btn btn-outline-secondary" disabled={busy} onClick={() => onClose(false)}>Cancel</button>
        <button type="button" className="btn btn-danger" disabled={busy || !ready} onClick={confirm}>
          {busy ? "Deleting…" : "Delete permanently"}
        </button>
      </div>
    </dialog>
  );
}

export const plural = (count, word) => `${count} ${word}${count === 1 ? "" : "s"}`;
